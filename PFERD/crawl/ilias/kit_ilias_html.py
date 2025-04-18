import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Dict, Optional, Union, cast
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from PFERD.logging import log
from PFERD.utils import url_set_query_params

TargetType = Union[str, int]


class IliasElementType(Enum):
    BOOKING = "booking"
    COURSE = "course"
    EXERCISE = "exercise"
    EXERCISE_FILES = "exercise_files"  # own submitted files
    FILE = "file"
    FOLDER = "folder"
    FORUM = "forum"
    FORUM_THREAD = "forum_thread"
    INFO_TAB = "info_tab"
    LEARNING_MODULE = "learning_module"
    LINK = "link"
    MEDIACAST_VIDEO = "mediacast_video"
    MEDIACAST_VIDEO_FOLDER = "mediacast_video_folder"
    MEETING = "meeting"
    MOB_VIDEO = "mob_video"
    OPENCAST_VIDEO = "opencast_video"
    OPENCAST_VIDEO_FOLDER = "opencast_video_folder"
    OPENCAST_VIDEO_FOLDER_MAYBE_PAGINATED = "opencast_video_folder_maybe_paginated"
    OPENCAST_VIDEO_PLAYER = "opencast_video_player"
    SCORM_LEARNING_MODULE = "scorm_learning_module"
    SURVEY = "survey"
    TEST = "test"  # an online test. Will be ignored currently.


@dataclass
class IliasPageElement:
    type: IliasElementType
    url: str
    name: str
    mtime: Optional[datetime] = None
    description: Optional[str] = None

    def id(self) -> str:
        regexes = [
            r"eid=(?P<id>[0-9a-z\-]+)",
            r"file/(?P<id>\d+)",
            r"copa/(?P<id>\d+)",
            r"fold/(?P<id>\d+)",
            r"frm/(?P<id>\d+)",
            r"exc/(?P<id>\d+)",
            r"thr_pk=(?P<id>\d+)",  # forums
            r"ref_id=(?P<id>\d+)",
            r"target=[a-z]+_(?P<id>\d+)",
            r"mm/(?P<id>\d+)",
            r"grp/(?P<id>\d+)"
        ]

        for regex in regexes:
            if match := re.search(regex, self.url):
                return match.groupdict()["id"]

        # Fall back to URL
        log.warn(f"Didn't find identity for {self.name} - {self.url}. Please report this.")
        return self.url

    @staticmethod
    def create_new(
        typ: IliasElementType,
        url: str,
        name: str,
        mtime: Optional[datetime] = None,
        description: Optional[str] = None,
        skip_sanitize: bool = False
    ) -> 'IliasPageElement':
        if typ == IliasElementType.MEETING:
            normalized = IliasPageElement._normalize_meeting_name(name)
            log.explain(f"Normalized meeting name from {name!r} to {normalized!r}")
            name = normalized

        if not skip_sanitize:
            name = _sanitize_path_name(name)

        return IliasPageElement(typ, url, name, mtime, description)

    @staticmethod
    def _normalize_meeting_name(meeting_name: str) -> str:
        """
        Normalizes meeting names, which have a relative time as their first part,
        to their date in ISO format.
        """

        # This checks whether we can reach a `:` without passing a `-`
        if re.search(r"^[^-]+: ", meeting_name):
            # Meeting name only contains date: "05. Jan 2000:"
            split_delimiter = ":"
        else:
            # Meeting name contains date and start/end times: "05. Jan 2000, 16:00 - 17:30:"
            split_delimiter = ", "

        # We have a meeting day without time
        date_portion_str = meeting_name.split(split_delimiter)[0]
        date_portion = demangle_date(date_portion_str)

        # We failed to parse the date, bail out
        if not date_portion:
            return meeting_name

        # Replace the first section with the absolute date
        rest_of_name = split_delimiter.join(meeting_name.split(split_delimiter)[1:])
        return datetime.strftime(date_portion, "%Y-%m-%d") + split_delimiter + rest_of_name


@dataclass
class IliasDownloadForumData:
    url: str
    form_data: Dict[str, Union[str, list[str]]]
    empty: bool


@dataclass
class IliasForumThread:
    name: str
    name_tag: Tag
    content_tag: Tag
    mtime: Optional[datetime]


@dataclass
class IliasLearningModulePage:
    title: str
    content: Tag
    next_url: Optional[str]
    previous_url: Optional[str]


class IliasPage:

    def __init__(self, soup: BeautifulSoup, _page_url: str, source_element: Optional[IliasPageElement]):
        self._soup = soup
        self._page_url = _page_url
        self._page_type = source_element.type if source_element else None
        self._source_name = source_element.name if source_element else ""

    @staticmethod
    def is_root_page(soup: BeautifulSoup) -> bool:
        if permalink := IliasPage.get_soup_permalink(soup):
            return "goto.php?target=root_" in permalink
        return False

    def get_child_elements(self) -> list[IliasPageElement]:
        """
        Return all child page elements you can find here.
        """
        if self._is_video_player():
            log.explain("Page is a video player, extracting URL")
            return self._player_to_video()
        if self._is_opencast_video_listing():
            log.explain("Page is an opencast video listing, searching for elements")
            return self._find_opencast_video_entries()
        if self._is_exercise_file():
            log.explain("Page is an exercise, searching for elements")
            return self._find_exercise_entries()
        if self._is_personal_desktop():
            log.explain("Page is the personal desktop, searching for elements")
            return self._find_personal_desktop_entries()
        if self._is_content_page():
            log.explain("Page is a content page, searching for elements")
            return self._find_copa_entries()
        if self._is_info_tab():
            log.explain("Page is info tab, searching for elements")
            return self._find_info_tab_entries()
        log.explain("Page is a normal folder, searching for elements")
        return self._find_normal_entries()

    def get_info_tab(self) -> Optional[IliasPageElement]:
        tab: Optional[Tag] = cast(Optional[Tag], self._soup.find(
            name="a",
            attrs={"href": lambda x: x is not None and "cmdClass=ilinfoscreengui" in x}
        ))
        if tab is not None:
            return IliasPageElement.create_new(
                IliasElementType.INFO_TAB,
                self._abs_url_from_link(tab),
                "infos"
            )
        return None

    def get_description(self) -> Optional[BeautifulSoup]:
        def is_interesting_class(name: str) -> bool:
            return name in ["ilCOPageSection", "ilc_Paragraph", "ilc_va_ihcap_VAccordIHeadCap"]

        paragraphs: list[Tag] = cast(list[Tag], self._soup.find_all(class_=is_interesting_class))
        if not paragraphs:
            return None

        # Extract bits and pieces into a string and parse it again.
        # This ensures we don't miss anything and weird structures are resolved
        # somewhat gracefully.
        raw_html = ""
        for p in paragraphs:
            if p.find_parent(class_=is_interesting_class):
                continue

            # Ignore special listings (like folder groupings)
            if "ilc_section_Special" in p["class"]:
                continue

            raw_html += str(p) + "\n"
        raw_html = f"<body>\n{raw_html}\n</body>"

        return BeautifulSoup(raw_html, "html.parser")

    def get_learning_module_data(self) -> Optional[IliasLearningModulePage]:
        if not self._is_learning_module_page():
            return None
        content = cast(Tag, self._soup.select_one("#ilLMPageContent"))
        title = cast(Tag, self._soup.select_one(".ilc_page_title_PageTitle")).get_text().strip()
        return IliasLearningModulePage(
            title=title,
            content=content,
            next_url=self._find_learning_module_next(),
            previous_url=self._find_learning_module_prev()
        )

    def _find_learning_module_next(self) -> Optional[str]:
        for link in self._soup.select("a.ilc_page_rnavlink_RightNavigationLink"):
            url = self._abs_url_from_link(link)
            if "baseClass=ilLMPresentationGUI" not in url:
                continue
            return url
        return None

    def _find_learning_module_prev(self) -> Optional[str]:
        for link in self._soup.select("a.ilc_page_lnavlink_LeftNavigationLink"):
            url = self._abs_url_from_link(link)
            if "baseClass=ilLMPresentationGUI" not in url:
                continue
            return url
        return None

    def get_forum_entries(self) -> list[IliasPageElement]:
        form = self._get_forum_form()
        if not form:
            return []
        threads = []

        for row in cast(list[Tag], form.select("table > tbody > tr")):
            url_tag = cast(
                Optional[Tag],
                row.find(name="a", attrs={"href": lambda x: x is not None and "cmd=viewthread" in x.lower()})
            )
            if url_tag is None:
                log.explain(f"Skipping row without URL: {row}")
                continue
            name = url_tag.get_text().strip()
            columns = [td.get_text().strip() for td in cast(list[Tag], row.find_all(name="td"))]
            potential_dates_opt = [IliasPage._find_date_in_text(column) for column in columns]
            potential_dates = [x for x in potential_dates_opt if x is not None]
            mtime = max(potential_dates) if potential_dates else None

            threads.append(IliasPageElement.create_new(
                IliasElementType.FORUM_THREAD,
                self._abs_url_from_link(url_tag),
                name,
                mtime=mtime
            ))

        return threads

    def get_download_forum_data(self, thread_ids: list[str]) -> Optional[IliasDownloadForumData]:
        form = cast(Optional[Tag], self._soup.find(
            "form",
            attrs={"action": lambda x: x is not None and "fallbackCmd=showThreads" in x}
        ))
        if not form:
            return None
        post_url = self._abs_url_from_relative(cast(str, form["action"]))

        log.explain(f"Fetching forum threads {thread_ids}")

        form_data: Dict[str, Union[str, list[str]]] = {
            "thread_ids[]": cast(list[str], thread_ids),
            "selected_cmd2": "html",
            "select_cmd2": "Ausführen",
            "selected_cmd": "",
        }

        return IliasDownloadForumData(url=post_url, form_data=form_data, empty=len(thread_ids) == 0)

    def _get_forum_form(self) -> Optional[Tag]:
        return cast(Optional[Tag], self._soup.find(
            "form",
            attrs={"action": lambda x: x is not None and "fallbackCmd=showThreads" in x}
        ))

    def get_next_stage_element(self) -> Optional[IliasPageElement]:
        if self._is_forum_page():
            if "trows=" in self._page_url:
                log.explain("Manual row override detected, accepting it as good")
                return None
            log.explain("Requesting *all* forum threads")
            thread_count = self._get_forum_thread_count()
            if thread_count is not None and thread_count > 400:
                log.warn(
                    "Forum has more than 400 threads, fetching all threads will take a while. "
                    "You might need to adjust your http_timeout config option."
                )

            # Fetch at least 400 in case we detect it wrong
            if thread_count is not None and thread_count < 400:
                thread_count = 400

            return self._get_show_max_forum_entries_per_page_url(thread_count)
        if self._is_ilias_opencast_embedding():
            log.explain("Unwrapping opencast embedding")
            return self.get_child_elements()[0]
        if self._page_type == IliasElementType.OPENCAST_VIDEO_FOLDER_MAYBE_PAGINATED:
            log.explain("Unwrapping video pagination")
            return self._find_opencast_video_entries_paginated()[0]
        if self._contains_collapsed_future_meetings():
            log.explain("Requesting *all* future meetings")
            return self._uncollapse_future_meetings_url()
        if not self._is_content_tab_selected():
            if self._page_type != IliasElementType.INFO_TAB:
                log.explain("Selecting content tab")
                return self._select_content_page_url()
            else:
                log.explain("Crawling info tab, skipping content select")
        return None

    def _is_forum_page(self) -> bool:
        if perma_link := self.get_permalink():
            return "target=frm_" in perma_link
        return False

    def _is_video_player(self) -> bool:
        return "paella_config_file" in str(self._soup)

    def _is_opencast_video_listing(self) -> bool:
        if self._is_ilias_opencast_embedding():
            return True

        # Raw listing without ILIAS fluff
        video_element_table = self._soup.find(
            name="table", id=re.compile(r"tbl_xoct_.+")
        )
        return video_element_table is not None

    def _is_ilias_opencast_embedding(self) -> bool:
        # ILIAS fluff around the real opencast html
        if self._soup.find(id="headerimage"):
            element: Tag = cast(Tag, self._soup.find(id="headerimage"))
            if "opencast" in cast(str, element.attrs["src"]).lower():
                return True
        return False

    def _is_exercise_file(self) -> bool:
        # we know it from before
        if self._page_type == IliasElementType.EXERCISE:
            return True

        # We have no suitable parent - let's guesss
        if self._soup.find(id="headerimage"):
            element: Tag = cast(Tag, self._soup.find(id="headerimage"))
            if "exc" in cast(str, element.attrs["src"]).lower():
                return True

        return False

    def _is_personal_desktop(self) -> bool:
        return "baseclass=ildashboardgui" in self._page_url.lower() and "&cmd=show" in self._page_url.lower()

    def _is_content_page(self) -> bool:
        if link := self.get_permalink():
            return "target=copa_" in link
        return False

    def _is_learning_module_page(self) -> bool:
        if link := self.get_permalink():
            return "target=pg_" in link
        return False

    def _contains_collapsed_future_meetings(self) -> bool:
        return self._uncollapse_future_meetings_url() is not None

    def _uncollapse_future_meetings_url(self) -> Optional[IliasPageElement]:
        element = cast(Optional[Tag], self._soup.find(
            "a",
            attrs={"href": lambda x: x is not None and ("crs_next_sess=1" in x or "crs_prev_sess=1" in x)}
        ))
        if not element:
            return None
        link = self._abs_url_from_link(element)
        return IliasPageElement.create_new(IliasElementType.FOLDER, link, "show all meetings")

    def _is_content_tab_selected(self) -> bool:
        return self._select_content_page_url() is None

    def _is_info_tab(self) -> bool:
        might_be_info = self._soup.find("form", attrs={"name": lambda x: x == "formInfoScreen"}) is not None
        return self._page_type == IliasElementType.INFO_TAB and might_be_info

    def _is_course_overview_page(self) -> bool:
        return "baseClass=ilmembershipoverviewgui" in self._page_url

    def _select_content_page_url(self) -> Optional[IliasPageElement]:
        tab = cast(Optional[Tag], self._soup.find(
            id="tab_view_content",
            attrs={"class": lambda x: x is not None and "active" not in x}
        ))
        # Already selected (or not found)
        if not tab:
            return None
        link = cast(Optional[Tag], tab.find("a"))
        if link:
            link_str = self._abs_url_from_link(link)
            return IliasPageElement.create_new(IliasElementType.FOLDER, link_str, "select content page")

        _unexpected_html_warning()
        log.warn_contd(f"Could not find content tab URL on {self._page_url!r}.")
        log.warn_contd("PFERD might not find content on the course's main page.")
        return None

    def _player_to_video(self) -> list[IliasPageElement]:
        # Fetch the actual video page. This is a small wrapper page initializing a javscript
        # player. Sadly we can not execute that JS. The actual video stream url is nowhere
        # on the page, but defined in a JS object inside a script tag, passed to the player
        # library.
        # We do the impossible and RegEx the stream JSON object out of the page's HTML source
        regex = re.compile(
            r"({\"streams\"[\s\S]+?),\s*{\"paella_config_file", re.IGNORECASE
        )
        json_match = regex.search(str(self._soup))

        if json_match is None:
            log.warn("Could not find JSON stream info in video player. Ignoring video.")
            return []
        json_str = json_match.group(1)

        # parse it
        json_object = json.loads(json_str)
        streams = [stream for stream in json_object["streams"]]

        # and just fetch the lone video url!
        if len(streams) == 1:
            video_url = streams[0]["sources"]["mp4"][0]["src"]
            return [
                IliasPageElement.create_new(IliasElementType.OPENCAST_VIDEO, video_url, self._source_name)
            ]

        log.explain(f"Found multiple videos for stream at {self._source_name}")
        items = []
        for stream in sorted(streams, key=lambda stream: stream["content"]):
            full_name = f"{self._source_name.replace('.mp4', '')} ({stream['content']}).mp4"
            video_url = stream["sources"]["mp4"][0]["src"]
            items.append(IliasPageElement.create_new(IliasElementType.OPENCAST_VIDEO, video_url, full_name))

        return items

    def _get_show_max_forum_entries_per_page_url(
        self, wanted_max: Optional[int] = None
    ) -> Optional[IliasPageElement]:
        correct_link = cast(Optional[Tag], self._soup.find(
            "a",
            attrs={"href": lambda x: x is not None and "trows=800" in x and "cmd=showThreads" in x}
        ))

        if not correct_link:
            return None

        link = self._abs_url_from_link(correct_link)
        if wanted_max is not None:
            link = link.replace("trows=800", f"trows={wanted_max}")

        return IliasPageElement.create_new(IliasElementType.FORUM, link, "show all forum threads")

    def _get_forum_thread_count(self) -> Optional[int]:
        log.explain_topic("Trying to find forum thread count")

        candidates = cast(list[Tag], self._soup.select(".ilTableFootLight"))
        extract_regex = re.compile(r"\s(?P<max>\d+)\s*\)")

        for candidate in candidates:
            log.explain(f"Found thread count candidate: {candidate}")
            if match := extract_regex.search(candidate.get_text()):
                return int(match.group("max"))
        else:
            log.explain("Found no candidates to extract thread count from")

        return None

    def _find_personal_desktop_entries(self) -> list[IliasPageElement]:
        items: list[IliasPageElement] = []

        titles: list[Tag] = self._soup.select("#block_pditems_0 .il-item-title")
        for title in titles:
            link = cast(Optional[Tag], title.find("a"))

            if not link:
                log.explain(f"Skipping offline item: {title.get_text().strip()!r}")
                continue

            name = _sanitize_path_name(link.text.strip())
            url = self._abs_url_from_link(link)

            if "cmd=manage" in url and "cmdClass=ilPDSelectedItemsBlockGUI" in url:
                # Configure button/link does not have anything interesting
                continue

            type = self._find_type_from_link(name, link, url)
            if not type:
                _unexpected_html_warning()
                log.warn_contd(f"Could not extract type for {link}")
                continue

            log.explain(f"Found {name!r}")

            if type == IliasElementType.FILE and "_download" not in url:
                url = re.sub(r"(target=file_\d+)", r"\1_download", url)
                log.explain("Rewired file URL to include download part")

            items.append(IliasPageElement.create_new(type, url, name))

        return items

    def _find_copa_entries(self) -> list[IliasPageElement]:
        items: list[IliasPageElement] = []
        links: list[Tag] = cast(list[Tag], self._soup.find_all(class_="ilc_flist_a_FileListItemLink"))

        for link in links:
            url = self._abs_url_from_link(link)
            name = re.sub(r"\([\d,.]+ [MK]B\)", "", link.get_text()).strip().replace("\t", "")
            name = _sanitize_path_name(name)

            if "file_id" not in url:
                _unexpected_html_warning()
                log.warn_contd(f"Found unknown content page item {name!r} with url {url!r}")
                continue

            items.append(IliasPageElement.create_new(IliasElementType.FILE, url, name))

        return items

    def _find_info_tab_entries(self) -> list[IliasPageElement]:
        items = []
        links: list[Tag] = self._soup.select("a.il_ContainerItemCommand")

        for link in links:
            if "cmdClass=ilobjcoursegui" not in link["href"]:
                continue
            if "cmd=sendfile" not in link["href"]:
                continue
            items.append(IliasPageElement.create_new(
                IliasElementType.FILE,
                self._abs_url_from_link(link),
                _sanitize_path_name(link.get_text())
            ))

        return items

    def _find_opencast_video_entries(self) -> list[IliasPageElement]:
        # ILIAS has three stages for video pages
        # 1. The initial dummy page without any videos. This page contains the link to the listing
        # 2. The video listing which might be paginated
        # 3. An unpaginated video listing (or at least one that includes 800 videos)
        #
        # We need to figure out where we are.

        video_element_table = cast(Optional[Tag], self._soup.find(
            name="table", id=re.compile(r"tbl_xoct_.+")
        ))

        if video_element_table is None:
            # We are in stage 1
            # The page is actually emtpy but contains the link to stage 2
            content_link: Tag = cast(Tag, self._soup.select_one("#tab_series a"))
            url: str = self._abs_url_from_link(content_link)
            query_params = {"limit": "800", "cmd": "asyncGetTableGUI", "cmdMode": "asynch"}
            url = url_set_query_params(url, query_params)
            log.explain("Found ILIAS video frame page, fetching actual content next")
            return [
                IliasPageElement.create_new(IliasElementType.OPENCAST_VIDEO_FOLDER_MAYBE_PAGINATED, url, "")
            ]

        is_paginated = self._soup.find(id=re.compile(r"tab_page_sel.+")) is not None

        if is_paginated and not self._page_type == IliasElementType.OPENCAST_VIDEO_FOLDER:
            # We are in stage 2 - try to break pagination
            return self._find_opencast_video_entries_paginated()

        return self._find_opencast_video_entries_no_paging()

    def _find_opencast_video_entries_paginated(self) -> list[IliasPageElement]:
        table_element = cast(Optional[Tag], self._soup.find(name="table", id=re.compile(r"tbl_xoct_.+")))

        if table_element is None:
            log.warn("Couldn't increase elements per page (table not found). I might miss elements.")
            return self._find_opencast_video_entries_no_paging()

        id_match = re.match(r"tbl_xoct_(.+)", cast(str, table_element.attrs["id"]))
        if id_match is None:
            log.warn("Couldn't increase elements per page (table id not found). I might miss elements.")
            return self._find_opencast_video_entries_no_paging()

        table_id = id_match.group(1)

        query_params = {f"tbl_xoct_{table_id}_trows": "800",
                        "cmd": "asyncGetTableGUI", "cmdMode": "asynch"}
        url = url_set_query_params(self._page_url, query_params)

        log.explain("Disabled pagination, retrying folder as a new entry")
        return [IliasPageElement.create_new(IliasElementType.OPENCAST_VIDEO_FOLDER, url, "")]

    def _find_opencast_video_entries_no_paging(self) -> list[IliasPageElement]:
        """
        Crawls the "second stage" video page. This page contains the actual video urls.
        """
        # Video start links are marked with an "Abspielen" link
        video_links = cast(list[Tag], self._soup.find_all(
            name="a", text=re.compile(r"\s*(Abspielen|Play)\s*")
        ))

        results: list[IliasPageElement] = []

        for link in video_links:
            results.append(self._listed_opencast_video_to_element(link))

        return results

    def _listed_opencast_video_to_element(self, link: Tag) -> IliasPageElement:
        # The link is part of a table with multiple columns, describing metadata.
        # 6th or 7th child (1 indexed) is the modification time string. Try to find it
        # by parsing backwards from the end and finding something that looks like a date
        modification_time = None
        row: Tag = link.parent.parent.parent  # type: ignore
        column_count = len(row.select("td.std"))
        for index in range(column_count, 0, -1):
            modification_string = link.parent.parent.parent.select_one(  # type: ignore
                f"td.std:nth-child({index})"
            ).get_text().strip()
            if match := re.search(r"\d+\.\d+.\d+ \d+:\d+", modification_string):
                modification_time = datetime.strptime(match.group(0), "%d.%m.%Y %H:%M")
                break

        if modification_time is None:
            log.warn(f"Could not determine upload time for {link}")
            modification_time = datetime.now()

        title = link.parent.parent.parent.select_one("td.std:nth-child(3)").get_text().strip()  # type: ignore
        title += ".mp4"

        video_name: str = _sanitize_path_name(title)

        video_url = self._abs_url_from_link(link)

        log.explain(f"Found video {video_name!r} at {video_url}")
        return IliasPageElement.create_new(
            IliasElementType.OPENCAST_VIDEO_PLAYER, video_url, video_name, modification_time
        )

    def _find_exercise_entries(self) -> list[IliasPageElement]:
        if self._soup.find(id="tab_submission"):
            log.explain("Found submission tab. This is an exercise detail page")
            return self._find_exercise_entries_detail_page()
        log.explain("Found no submission tab. This is an exercise root page")
        return self._find_exercise_entries_root_page()

    def _find_exercise_entries_detail_page(self) -> list[IliasPageElement]:
        results: list[IliasPageElement] = []

        # Find all download links in the container (this will contain all the files)
        download_links = cast(list[Tag], self._soup.find_all(
            name="a",
            # download links contain the given command class
            attrs={"href": lambda x: x is not None and "cmd=download" in x},
            text="Download"
        ))

        for link in download_links:
            parent_row: Tag = cast(Tag, link.find_parent("tr"))
            children = cast(list[Tag], parent_row.find_all("td"))

            name = _sanitize_path_name(children[1].get_text().strip())
            log.explain(f"Found exercise detail entry {name!r}")

            date = None
            for child in reversed(children):
                date = demangle_date(child.get_text().strip(), fail_silently=True)
                if date is not None:
                    break
            if date is None:
                log.warn(f"Date parsing failed for exercise entry {name!r}")

            results.append(IliasPageElement.create_new(
                IliasElementType.FILE,
                self._abs_url_from_link(link),
                name,
                date
            ))

        return results

    def _find_exercise_entries_root_page(self) -> list[IliasPageElement]:
        results: list[IliasPageElement] = []

        # Each assignment is in an accordion container
        assignment_containers: list[Tag] = self._soup.select(".il_VAccordionInnerContainer")

        for container in assignment_containers:
            # Fetch the container name out of the header to use it in the path
            container_name = cast(Tag, container.select_one(".ilAssignmentHeader")).get_text().strip()
            log.explain(f"Found exercise container {container_name!r}")

            # Find all download links in the container (this will contain all the files)
            files = cast(list[Tag], container.find_all(
                name="a",
                # download links contain the given command class
                attrs={"href": lambda x: x is not None and "cmdClass=ilexsubmissiongui" in x},
                text="Download"
            ))

            # Grab each file as you now have the link
            for file_link in files:
                # Two divs, side by side. Left is the name, right is the link ==> get left
                # sibling
                file_name = cast(
                    Tag,
                    cast(Tag, file_link.parent).find_previous(name="div")
                ).get_text().strip()
                url = self._abs_url_from_link(file_link)

                log.explain(f"Found exercise entry {file_name!r}")
                results.append(IliasPageElement.create_new(
                    IliasElementType.FILE,
                    url,
                    _sanitize_path_name(container_name) + "/" + _sanitize_path_name(file_name),
                    mtime=None,  # We do not have any timestamp
                    skip_sanitize=True
                ))

            # Find all links to file listings (e.g. "Submitted Files" for groups)
            file_listings = cast(list[Tag], container.find_all(
                name="a",
                # download links contain the given command class
                attrs={"href": lambda x: x is not None and "cmdclass=ilexsubmissionfilegui" in x.lower()}
            ))

            # Add each listing as a new
            for listing in file_listings:
                parent_container = cast(Tag, listing.find_parent(
                    "div", attrs={"class": lambda x: x is not None and "form-group" in x}
                ))
                label_container = cast(Tag, parent_container.find(
                    attrs={"class": lambda x: x is not None and "control-label" in x}
                ))
                file_name = label_container.get_text().strip()
                url = self._abs_url_from_link(listing)
                log.explain(f"Found exercise detail {file_name!r} at {url}")
                results.append(IliasPageElement.create_new(
                    IliasElementType.EXERCISE_FILES,
                    url,
                    _sanitize_path_name(container_name) + "/" + _sanitize_path_name(file_name),
                    None,  # we do not have any timestamp
                    skip_sanitize=True
                ))

        return results

    def _find_normal_entries(self) -> list[IliasPageElement]:
        result: list[IliasPageElement] = []

        links: list[Tag] = []
        # Fetch all links and throw them to the general interpreter
        if self._is_course_overview_page():
            log.explain("Page is a course overview page, adjusting link selector")
            links.extend(self._soup.select(".il-item-title > a"))
        else:
            links.extend(self._soup.select("a.il_ContainerItemTitle"))

        for link in links:
            abs_url = self._abs_url_from_link(link)
            # Make sure parents are sanitized. We do not want accidental parents
            parents = [_sanitize_path_name(x) for x in self._find_upwards_folder_hierarchy(link)]

            if parents:
                element_name = "/".join(parents) + "/" + _sanitize_path_name(link.get_text())
            else:
                element_name = _sanitize_path_name(link.get_text())

            element_type = self._find_type_from_link(element_name, link, abs_url)
            description = self._find_link_description(link)

            # The last meeting on every page is expanded by default.
            # Its content is then shown inline *and* in the meeting page itself.
            # We should skip the inline content.
            if element_type != IliasElementType.MEETING and self._is_in_expanded_meeting(link):
                continue

            if not element_type:
                continue
            elif element_type == IliasElementType.FILE:
                result.append(self._file_to_element(element_name, abs_url, link))
                continue

            log.explain(f"Found {element_name!r}")
            result.append(IliasPageElement.create_new(
                element_type,
                abs_url,
                element_name,
                description=description,
                skip_sanitize=True
            ))

        result += self._find_cards()
        result += self._find_mediacast_videos()
        result += self._find_mob_videos()

        return result

    def _find_mediacast_videos(self) -> list[IliasPageElement]:
        videos: list[IliasPageElement] = []

        for elem in cast(list[Tag], self._soup.select(".ilPlayerPreviewOverlayOuter")):
            element_name = _sanitize_path_name(
                cast(Tag, elem.select_one(".ilPlayerPreviewDescription")).get_text().strip()
            )
            if not element_name.endswith(".mp4"):
                # just to make sure it has some kinda-alrightish ending
                element_name = element_name + ".mp4"
            video_element = cast(Optional[Tag], elem.find(name="video"))
            if not video_element:
                _unexpected_html_warning()
                log.warn_contd(f"No <video> element found for mediacast video '{element_name}'")
                continue

            videos.append(IliasPageElement.create_new(
                typ=IliasElementType.MEDIACAST_VIDEO,
                url=self._abs_url_from_relative(cast(str, video_element.get("src"))),
                name=element_name,
                mtime=self._find_mediacast_video_mtime(cast(Tag, elem.find_parent(name="td")))
            ))

        return videos

    def _find_mob_videos(self) -> list[IliasPageElement]:
        videos: list[IliasPageElement] = []

        for figure in self._soup.select("figure.ilc_media_cont_MediaContainerHighlighted"):
            title = cast(Tag, figure.select_one("figcaption")).get_text().strip() + ".mp4"
            video_element = figure.select_one("video")
            if not video_element:
                _unexpected_html_warning()
                log.warn_contd(f"No <video> element found for mob video '{title}'")
                continue

            url = None
            for source in video_element.select("source"):
                if source.get("type", "") == "video/mp4":
                    url = cast(Optional[str], source.get("src"))
                    break

            if url is None:
                _unexpected_html_warning()
                log.warn_contd(f"No <source> element found for mob video '{title}'")
                continue

            videos.append(IliasPageElement.create_new(
                typ=IliasElementType.MOB_VIDEO,
                url=self._abs_url_from_relative(url),
                name=_sanitize_path_name(title),
                mtime=None
            ))

        return videos

    def _find_mediacast_video_mtime(self, enclosing_td: Tag) -> Optional[datetime]:
        description_td = cast(Tag, enclosing_td.find_previous_sibling("td"))
        if not description_td:
            return None

        meta_tag = cast(Optional[Tag], description_td.find_all("p")[-1])
        if not meta_tag:
            return None

        updated_str = meta_tag.get_text().strip().replace("\n", " ")
        updated_str = re.sub(".+?: ", "", updated_str)
        return demangle_date(updated_str)

    def _is_in_expanded_meeting(self, tag: Tag) -> bool:
        """
        Returns whether a file is part of an expanded meeting.
        Has false positives for meetings themselves as their title is also "in the expanded meeting content".
        It is in the same general div and this whole thing is guesswork.
        Therefore, you should check for meetings before passing them in this function.
        """
        parents: list[Tag] = list(tag.parents)
        for parent in parents:
            if not parent.get("class"):
                continue

            # We should not crawl files under meetings
            if "ilContainerListItemContentCB" in cast(str, parent.get("class")):
                link: Tag = parent.parent.find("a")  # type: ignore
                type = IliasPage._find_type_from_folder_like(link, self._page_url)
                return type == IliasElementType.MEETING

        return False

    def _find_upwards_folder_hierarchy(self, tag: Tag) -> list[str]:
        """
        Interprets accordions and expandable blocks as virtual folders and returns them
        in order. This allows us to find a file named "Test" in an accordion "Acc" as "Acc/Test"
        """
        found_titles = []

        outer_accordion_content: Optional[Tag] = None

        parents: list[Tag] = list(tag.parents)
        for parent in parents:
            if not parent.get("class"):
                continue

            # ILIAS has proper accordions and weird blocks that look like normal headings,
            # but some JS later transforms them into an accordion.

            # This is for these weird JS-y blocks and custom item groups
            if "ilContainerItemsContainer" in cast(str, parent.get("class")):
                data_store_url = parent.parent.get("data-store-url", "").lower()  # type: ignore
                is_custom_item_group = "baseclass=ilcontainerblockpropertiesstoragegui" in data_store_url \
                                       and "cont_block_id=" in data_store_url
                # I am currently under the impression that *only* those JS blocks have an
                # ilNoDisplay class.
                if not is_custom_item_group and "ilNoDisplay" not in cast(str, parent.get("class")):
                    continue
                prev = cast(Tag, parent.find_previous_sibling("div"))
                if "ilContainerBlockHeader" in cast(str, prev.get("class")):
                    if prev.find("h3"):
                        found_titles.append(cast(Tag, prev.find("h3")).get_text().strip())
                    else:
                        found_titles.append(cast(Tag, prev.find("h2")).get_text().strip())

            # And this for real accordions
            if "il_VAccordionContentDef" in cast(str, parent.get("class")):
                outer_accordion_content = parent
                break

        if outer_accordion_content:
            accordion_tag = cast(Tag, outer_accordion_content.parent)
            head_tag = cast(Tag, accordion_tag.find(attrs={
                "class": lambda x: x is not None and "ilc_va_ihead_VAccordIHead" in x
            }))
            found_titles.append(head_tag.get_text().strip())

        return [_sanitize_path_name(x) for x in reversed(found_titles)]

    def _find_link_description(self, link: Tag) -> Optional[str]:
        tile = cast(
            Tag,
            link.find_parent("div", {"class": lambda x: x is not None and "il_ContainerListItem" in x})
        )
        if not tile:
            return None
        description_element = cast(
            Tag,
            tile.find("div", {"class": lambda x: x is not None and "il_Description" in x})
        )
        if not description_element:
            return None
        return description_element.get_text().strip()

    def _file_to_element(self, name: str, url: str, link_element: Tag) -> IliasPageElement:
        # Files have a list of properties (type, modification date, size, etc.)
        # In a series of divs.
        # Find the parent containing all those divs, so we can filter our what we need
        properties_parent = cast(Tag, cast(Tag, link_element.find_parent(
            "div", {"class": lambda x: "il_ContainerListItem" in x}
        )).select_one(".il_ItemProperties"))
        # The first one is always the filetype
        file_type = cast(Tag, properties_parent.select_one("span.il_ItemProperty")).get_text().strip()

        # The rest does not have a stable order. Grab the whole text and reg-ex the date
        # out of it
        all_properties_text = properties_parent.get_text().strip()
        modification_date = IliasPage._find_date_in_text(all_properties_text)
        if modification_date is None:
            log.explain(f"Element {name} at {url} has no date.")

        # Grab the name from the link text
        full_path = name + "." + file_type

        log.explain(f"Found file {full_path!r}")
        return IliasPageElement.create_new(
            IliasElementType.FILE, url, full_path, modification_date, skip_sanitize=True
        )

    def _find_cards(self) -> list[IliasPageElement]:
        result: list[IliasPageElement] = []

        card_titles: list[Tag] = self._soup.select(".card-title a")

        for title in card_titles:
            url = self._abs_url_from_link(title)
            name = _sanitize_path_name(title.get_text().strip())
            type = self._find_type_from_card(title)

            if not type:
                _unexpected_html_warning()
                log.warn_contd(f"Could not extract type for {title}")
                continue

            result.append(IliasPageElement.create_new(type, url, name))

        card_button_tiles: list[Tag] = self._soup.select(".card-title button")

        for button in card_button_tiles:
            regex = re.compile(button["id"] + r".*window.open\(['\"](.+?)['\"]")  # type: ignore
            res = regex.search(str(self._soup))
            if not res:
                _unexpected_html_warning()
                log.warn_contd(f"Could not find click handler target for {button}")
                continue
            url = self._abs_url_from_relative(res.group(1))
            name = _sanitize_path_name(button.get_text().strip())
            type = self._find_type_from_card(button)
            caption_parent = cast(Tag, button.find_parent(
                "div",
                attrs={"class": lambda x: x is not None and "caption" in x},
            ))
            caption_container = caption_parent.find_next_sibling("div")
            if caption_container:
                description = caption_container.get_text().strip()
            else:
                description = None

            if not type:
                _unexpected_html_warning()
                log.warn_contd(f"Could not extract type for {button}")
                continue

            result.append(IliasPageElement.create_new(type, url, name, description=description))

        return result

    def _find_type_from_card(self, card_title: Tag) -> Optional[IliasElementType]:
        def is_card_root(element: Tag) -> bool:
            return "il-card" in element["class"] and "thumbnail" in element["class"]

        card_root: Optional[Tag] = None

        # We look for the card root
        for parent in card_title.parents:
            if is_card_root(parent):
                card_root = parent
                break

        if card_root is None:
            _unexpected_html_warning()
            log.warn_contd(f"Tried to figure out element type, but did not find an icon for {card_title}")
            return None

        icon = cast(Tag, card_root.select_one(".il-card-repository-head .icon"))

        if "opencast" in icon["class"] or "xoct" in icon["class"]:
            return IliasElementType.OPENCAST_VIDEO_FOLDER_MAYBE_PAGINATED
        if "exc" in icon["class"]:
            return IliasElementType.EXERCISE
        if "grp" in icon["class"]:
            return IliasElementType.FOLDER
        if "webr" in icon["class"]:
            return IliasElementType.LINK
        if "book" in icon["class"]:
            return IliasElementType.BOOKING
        if "crsr" in icon["class"]:
            return IliasElementType.COURSE
        if "frm" in icon["class"]:
            return IliasElementType.FORUM
        if "sess" in icon["class"]:
            return IliasElementType.MEETING
        if "tst" in icon["class"]:
            return IliasElementType.TEST
        if "fold" in icon["class"]:
            return IliasElementType.FOLDER
        if "copa" in icon["class"]:
            return IliasElementType.FOLDER
        if "svy" in icon["class"]:
            return IliasElementType.SURVEY
        if "file" in icon["class"]:
            return IliasElementType.FILE
        if "mcst" in icon["class"]:
            return IliasElementType.MEDIACAST_VIDEO_FOLDER

        _unexpected_html_warning()
        log.warn_contd(f"Could not extract type from {icon} for card title {card_title}")
        return None

    @staticmethod
    def _find_type_from_link(
        element_name: str,
        link_element: Tag,
        url: str
    ) -> Optional[IliasElementType]:
        """
        Decides which sub crawler to use for a given top level element.
        """
        parsed_url = urlparse(url)

        # file URLs contain "target=file"
        if "target=file_" in parsed_url.query:
            return IliasElementType.FILE

        if "target=grp_" in parsed_url.query:
            return IliasElementType.FOLDER

        if "target=crs_" in parsed_url.query:
            return IliasElementType.FOLDER

        if "baseClass=ilExerciseHandlerGUI" in parsed_url.query:
            return IliasElementType.EXERCISE

        if "baseClass=ilLinkResourceHandlerGUI" in parsed_url.query and "calldirectlink" in parsed_url.query:
            return IliasElementType.LINK

        if "cmd=showThreads" in parsed_url.query or "target=frm_" in parsed_url.query:
            return IliasElementType.FORUM

        if "cmdClass=ilobjtestgui" in parsed_url.query:
            return IliasElementType.TEST

        if "baseClass=ilLMPresentationGUI" in parsed_url.query:
            return IliasElementType.LEARNING_MODULE

        if "baseClass=ilMediaCastHandlerGUI" in parsed_url.query:
            return IliasElementType.MEDIACAST_VIDEO_FOLDER

        if "baseClass=ilSAHSPresentationGUI" in parsed_url.query:
            return IliasElementType.SCORM_LEARNING_MODULE

        # other universities might have content type specified in URL path
        if "_file_" in parsed_url.path:
            return IliasElementType.FILE

        if "_fold_" in parsed_url.path or "_copa_" in parsed_url.path:
            return IliasElementType.FOLDER

        if "_frm_" in parsed_url.path:
            return IliasElementType.FORUM

        if "_exc_" in parsed_url.path:
            return IliasElementType.EXERCISE

        # Booking and Meeting can not be detected based on the link. They do have a ref_id though, so
        # try to guess it from the image.

        # Everything with a ref_id can *probably* be opened to reveal nested things
        # video groups, directories, exercises, etc
        if "ref_id=" in parsed_url.query or "goto.php" in parsed_url.path:
            return IliasPage._find_type_from_folder_like(link_element, url)

        _unexpected_html_warning()
        log.warn_contd(
            f"Tried to figure out element type, but failed for {element_name!r} / {link_element!r})"
        )
        return None

    @staticmethod
    def _find_type_from_folder_like(link_element: Tag, url: str) -> Optional[IliasElementType]:
        """
        Try crawling something that looks like a folder.
        """
        # pylint: disable=too-many-return-statements

        found_parent: Optional[Tag] = None

        # We look for the outer div of our inner link, to find information around it
        # (mostly the icon)
        for parent in link_element.parents:
            if "ilContainerListItemOuter" in parent["class"] or "il-std-item" in parent["class"]:
                found_parent = parent
                break

        if found_parent is None:
            _unexpected_html_warning()
            log.warn_contd(f"Tried to figure out element type, but did not find an icon for {url}")
            return None

        # Find the small descriptive icon to figure out the type
        img_tag: Optional[Tag] = found_parent.select_one("img.ilListItemIcon")

        if img_tag is None:
            img_tag = found_parent.select_one("img.icon")

        is_session_expansion_button = found_parent.find(
            "a",
            attrs={"href": lambda x: x is not None and ("crs_next_sess=" in x or "crs_prev_sess=" in x)}
        )
        if img_tag is None and is_session_expansion_button:
            log.explain("Found session expansion button, skipping it as it has no content")
            return None

        if img_tag is None:
            _unexpected_html_warning()
            log.warn_contd(f"Tried to figure out element type, but did not find an image for {url}")
            return None

        if "opencast" in str(img_tag["alt"]).lower():
            return IliasElementType.OPENCAST_VIDEO_FOLDER_MAYBE_PAGINATED

        if str(img_tag["src"]).endswith("icon_exc.svg"):
            return IliasElementType.EXERCISE

        if str(img_tag["src"]).endswith("icon_webr.svg"):
            return IliasElementType.LINK

        if str(img_tag["src"]).endswith("icon_book.svg"):
            return IliasElementType.BOOKING

        if str(img_tag["src"]).endswith("frm.svg"):
            return IliasElementType.FORUM

        if str(img_tag["src"]).endswith("sess.svg"):
            return IliasElementType.MEETING

        if str(img_tag["src"]).endswith("icon_tst.svg"):
            return IliasElementType.TEST

        if str(img_tag["src"]).endswith("icon_mcst.svg"):
            return IliasElementType.MEDIACAST_VIDEO_FOLDER

        if str(img_tag["src"]).endswith("icon_sahs.svg"):
            return IliasElementType.SCORM_LEARNING_MODULE

        return IliasElementType.FOLDER

    @staticmethod
    def is_logged_in(soup: BeautifulSoup) -> bool:
        # Normal ILIAS pages
        mainbar = cast(Optional[Tag], soup.find(class_="il-maincontrols-metabar"))
        if mainbar is not None:
            login_button = mainbar.find(attrs={"href": lambda x: x is not None and "login.php" in x})
            shib_login = soup.find(id="button_shib_login")
            print(login_button, shib_login)
            return not login_button and not shib_login

        # Personal Desktop
        if soup.find("a", attrs={"href": lambda x: x is not None and "block_type=pditems" in x}):
            return True

        # Empty personal desktop has zero (0) markers. Match on the text...
        if alert := soup.select_one(".alert-info"):
            text = alert.get_text().lower()
            if "you have not yet selected any favourites" in text:
                return True
            if "sie haben aktuell noch keine favoriten ausgewählt" in text:
                return True

        # Video listing embeds do not have complete ILIAS html. Try to match them by
        # their video listing table
        video_table = soup.find(
            recursive=True,
            name="table",
            attrs={"id": lambda x: x is not None and x.startswith("tbl_xoct")}
        )
        if video_table is not None:
            return True
        # The individual video player wrapper page has nothing of the above.
        # Match it by its playerContainer.
        if soup.select_one("#playerContainer") is not None:
            return True
        return False

    @staticmethod
    def _find_date_in_text(text: str) -> Optional[datetime]:
        modification_date_match = re.search(
            r"(((\d+\. \w+ \d+)|(Gestern|Yesterday)|(Heute|Today)|(Morgen|Tomorrow)), \d+:\d+)",
            text
        )
        if modification_date_match is not None:
            modification_date_str = modification_date_match.group(1)
            return demangle_date(modification_date_str)
        return None

    def get_permalink(self) -> Optional[str]:
        return IliasPage.get_soup_permalink(self._soup)

    def _abs_url_from_link(self, link_tag: Tag) -> str:
        """
        Create an absolute url from an <a> tag.
        """
        return self._abs_url_from_relative(cast(str, link_tag.get("href")))

    def _abs_url_from_relative(self, relative_url: str) -> str:
        """
        Create an absolute url from a relative URL.
        """
        return urljoin(self._page_url, relative_url)

    @staticmethod
    def get_soup_permalink(soup: BeautifulSoup) -> Optional[str]:
        # First try the old <a> tag pattern
        perma_link_element = soup.select_one(".il-footer-permanent-url > a")
        if perma_link_element and perma_link_element.get("href"):
            return perma_link_element.get("href")

        # If no <a> tag found, look for the new button pattern
        button = soup.select_one(".il-footer-permanent-url > button")
        if button:
            # Check for data attribute approach
            # if button.has_attr('data-url'):
            #     return button['data-url']
            #
            # # Check for onclick attribute approach
            # if button.has_attr('onclick'):
            #     match = re.search(re.compile("copyText\(['\"](.+?)['\"]\)"), button['onclick'])
            #     if match:
            #         return match.group(1).replace('\\/', '/')

            # Check for script-based approach
            button_id = button.get('id')
            if button_id:
                script = soup.find('script', string=re.compile(f'document.getElementById\("{button_id}"\)'))
                if script:
                    match = re.search(r'copyText\(["\'](.+?)["\']\)', script.string)
                    if match:
                        return match.group(1).replace('\\/', '/')

        return None


def _unexpected_html_warning() -> None:
    log.warn("Encountered unexpected HTML structure, ignoring element.")


german_months = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']
english_months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def demangle_date(date_str: str, fail_silently: bool = False) -> Optional[datetime]:
    """
    Demangle a given date in one of the following formats (hour/minute part is optional):
    "Gestern, HH:MM"
    "Heute, HH:MM"
    "Morgen, HH:MM"
    "dd. mon yyyy, HH:MM
    """
    try:
        # Normalize whitespace because users
        date_str = re.sub(r"\s+", " ", date_str)

        date_str = re.sub("Gestern|Yesterday", _format_date_english(_yesterday()), date_str, re.I)
        date_str = re.sub("Heute|Today", _format_date_english(date.today()), date_str, re.I)
        date_str = re.sub("Morgen|Tomorrow", _format_date_english(_tomorrow()), date_str, re.I)
        date_str = date_str.strip()
        for german, english in zip(german_months, english_months):
            date_str = date_str.replace(german, english)
            # Remove trailing dots for abbreviations, e.g. "20. Apr. 2020" -> "20. Apr 2020"
            date_str = date_str.replace(english + ".", english)

        # We now have a nice english String in the format: "dd. mmm yyyy, hh:mm" or "dd. mmm yyyy"

        # Check if we have a time as well
        if ", " in date_str:
            day_part, time_part = date_str.split(",")
        else:
            day_part = date_str.split(",")[0]
            time_part = None

        day_str, month_str, year_str = day_part.split(" ")

        day = int(day_str.strip().replace(".", ""))
        month = english_months.index(month_str.strip()) + 1
        year = int(year_str.strip())

        if time_part:
            hour_str, minute_str = time_part.split(":")
            hour = int(hour_str)
            minute = int(minute_str)
            return datetime(year, month, day, hour, minute)

        return datetime(year, month, day)
    except Exception:
        if not fail_silently:
            log.warn(f"Date parsing failed for {date_str!r}")
        return None


def _format_date_english(date_to_format: date) -> str:
    month = english_months[date_to_format.month - 1]
    return f"{date_to_format.day:02d}. {month} {date_to_format.year:04d}"


def _yesterday() -> date:
    return date.today() - timedelta(days=1)


def _tomorrow() -> date:
    return date.today() + timedelta(days=1)


def _sanitize_path_name(name: str) -> str:
    return name.replace("/", "-").replace("\\", "-").strip()


def parse_ilias_forum_export(forum_export: BeautifulSoup) -> list[IliasForumThread]:
    elements = []
    for p in forum_export.select("body > p"):
        title_tag = p
        content_tag = cast(Optional[Tag], p.find_next_sibling("ul"))

        title = cast(Tag, p.find("b")).text
        if ":" in title:
            title = title[title.find(":") + 1:]
        title = title.strip()

        if not content_tag or content_tag.find_previous_sibling("p") != title_tag:
            # ILIAS allows users to delete the initial post while keeping the thread open
            # This produces empty threads without *any* content.
            # I am not sure why you would want this, but ILIAS makes it easy to do.
            elements.append(IliasForumThread(title, title_tag, forum_export.new_tag("ul"), None))
            continue

        mtime = _guess_timestamp_from_forum_post_content(content_tag)
        elements.append(IliasForumThread(title, title_tag, content_tag, mtime))

    return elements


def _guess_timestamp_from_forum_post_content(content: Tag) -> Optional[datetime]:
    posts = cast(Optional[Tag], content.select(".ilFrmPostHeader > span.small"))
    if not posts:
        return None

    newest_date: Optional[datetime] = None

    for post in posts:
        text = post.text.strip()
        text = text[text.rfind("|") + 1:]
        date = demangle_date(text, fail_silently=True)
        if not date:
            continue

        if not newest_date or newest_date < date:
            newest_date = date

    return newest_date

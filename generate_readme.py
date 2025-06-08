"""Recreate the ``README.md`` file whenever this Python script runs."""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime
import enum
import itertools
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import typing
from urllib import parse

import requests
import tree_sitter
import tree_sitter_html
import tree_sitter_markdown

_ALL_PLUGINS_MARKER = "All Plugins"
_BULLETPOINT_EXPRESSION = re.compile("^\s*-\s*(?P<text>.+)$")
_CURRENT_DIRECTORY = os.path.dirname(os.path.realpath(__file__))
_DESCRIPTION_LENGTH = 80
_ENCODING = "utf-8"

_GITHUB_PATTERNS = (
    re.compile(r"^https?://github\.com/"),
    re.compile(r"^git@github\.com:"),
    re.compile(r"^git://github\.com/"),
)
_GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

_HTML_LANGUAGE = tree_sitter.Language(tree_sitter_html.language())
_MARKDOWN_LANGUAGE = tree_sitter.Language(tree_sitter_markdown.language())

_MARKDOWN_FENCE_MARKER = "```"

T = typing.TypeVar("T")
_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, kw_only=True)
class _Model:
    """An AI model, used to generate code and other text.

    Attributes:
        search_terms: The strings used to "find" the model in a plugin's documentation.
        name: The real, unabridged name of the model.
        url: The page online where you can learn more about the model.

    """

    search_terms: typing.Sequence[str] | str | None
    name: str
    url: str

    def get_search_terms(self) -> list[str]:
        """Get the string used to "find" the model in a plugin's documentation."""
        if not self.search_terms:
            return [self.name]

        if isinstance(self.search_terms, str):
            return [self.search_terms]

        return list(self.search_terms)

    def serialize_to_markdown_tag(self) -> str:
        """Link to where the user can learn more about the model."""
        return f"[#{self.name}]({self.url})"


_MODELS = (
    _Model(search_terms="claude", name="Claude", url="https://claude.ai"),
    _Model(search_terms="deepseek", name="DeepSeek", url="https://chat.deepseek.com"),
    _Model(search_terms="ollama", name="Ollama", url="https://ollama.com"),
    _Model(search_terms="openai", name="OpenAI", url="https://openai.com"),
    _Model(search_terms="tabnine", name="TabNine", url="https://www.tabnine.com"),
    _Model(
        search_terms=("codeium", "windsurf"),
        name="Windsurf",
        url="https://windsurf.com",
    ),
    _Model(search_terms=("codium", "qodo"), name="Qodo", url="https://www.qodo.ai"),
)


class _GitHubRepositoryDetailsLicense(typing.TypedDict):
    """A description of the "terms of use" for some git repository.

    Attributes:
        name: The name of the license, assuming it's a commonly-used license.
        url: The page where you can learn more about the license.

    """

    name: str
    url: str | None


class _GitHubRepositoryDetailsOwner(typing.TypedDict):
    """A nested struct that describes a user or organization that owns a repository."""

    login: str


class _GitHubRepositoryDetails(typing.TypedDict):
    """Summarized repository information from GitHub.

    Attributes:
        default_branch: Usually ``"master"`` or ``"main"``. It's the cloning branch.
        description: The user's chosen description, if any.
        html_str: The raw URL to the repository. (The clone URL).
        license: A description of the "terms of use" for some git repository.
        name: The name of the repository.
        owner: The user / organization that owns the repository.
        pushed_at: The user's last ``git push``. e.g. ``"2025-06-04T19:41:16Z"``.
        stargazers_count: The number of user stars, it's a 0-or-more value.

    """

    default_branch: str
    description: str
    html_url: str
    license: _GitHubRepositoryDetailsLicense | None
    name: str
    owner: _GitHubRepositoryDetailsOwner
    pushed_at: str
    stargazers_count: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class _GitHubRepositoryRequest:
    owner: str
    name: str

    @classmethod
    def from_url(cls, url: str) -> _GitHubRepositoryRequest:
        parsed = parse.urlparse(url)
        parts = parsed.path.split("/")
        owner = parts[-2]
        name = parts[-1]

        return cls(owner=owner, name=name)


class _GitHubTreeResult(typing.TypedDict):
    path: str
    type: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class _ParsedArguments:
    """The user's terminal input, parsed as Python object.

    Attributes:
        directory: The folder where any cloned / download artifacts will go under.

    """

    directory: str


class _Category(str, enum.Enum):
    """The expected "types" of AI plugin."""

    code_editting = "code-editting"
    auto_completion = "auto-completion"
    communication = "communication / chat"
    unknown = "unknown"


class _NodeWrapper:
    """A simple "method-chainer" class to make ``tree-sitter`` nodes easier to walk."""

    def __init__(self, node: tree_sitter.Node, data: bytes) -> None:
        """Keep track of a ``tree-sitter`` node and the ``data`` it can be found within.

        Args:
            node: Some parsed language ``tree-sitter`` data.
            data: The raw text / code / parsed thing.

        """
        super().__init__()

        self._node = node
        self._data = data

    def get(self, path: typing.Iterable[str | int] | str | int) -> _NodeWrapper:
        """Walk ``path`` for child ``tree-sitter`` nodes that match.

        Args:
            path:
                If it's a string, it must be type-name of some ``tree-sitter`` child node.
                If it's an int, it's an exact index to a ``tree-sitter`` child node.

        Raises:
            RuntimeError: If no child could be found.

        Returns:
            The inner child, returned as a wrapped node.

        """
        if isinstance(path, (str, int)):
            path = [path]

        current = self._node

        for name in path:
            if isinstance(name, str):
                child = _get_first_child_of_type(current, name)
            else:
                child = _verify(current.named_child(name))

            if not child:
                raise RuntimeError(f'Child "{name}" node not found in "{child}" node.')

            current = child

        return self.__class__(current, self._data)

    def text(self, path: typing.Iterable[str] | str | None = None) -> bytes:
        """Walk ``path`` and get its contents. Otherwise, get this instance's contents.

        Args:
            path: Some node-type path to look within for text.

        Returns:
            The found text.

        """
        if path:
            node = self.get(path)

            return node.text()

        return self._data[self._node.start_byte : self._node.end_byte]

    def __repr__(self) -> str:
        """Show how to reproduce this instance."""
        return f"{self.__class__.__name__}({self._node!r}, {self._data!r})"

    def __str__(self) -> str:
        """Show a concise, "human-readable" version of this instance."""
        return f"<{self.__class__.__name__} {self._node}>"


@dataclasses.dataclass(frozen=True, kw_only=True)
class _GitHubRow:
    """The data to serialize into GitHub markdown row text, later."""

    description: str | None
    last_commit_date: str
    license: _GitHubRepositoryDetailsLicense | None
    models: set[_Model]
    name: str
    star_count: int
    status: str | None
    url: str

    def get_repository_label(self) -> str:
        """Get a short link to the git repository."""
        return f"[{self.name}]({self.url})"


@dataclasses.dataclass(frozen=True, kw_only=True)
class _GitHubRepository:
    """A small abstraction over a git repository."""

    directory: str
    documentation: list[str]
    name: str
    owner: str
    url: str


class _Status(str, enum.Enum):
    wip = "wip"
    mature = "mature"


@dataclasses.dataclass(frozen=True, kw_only=True)
class _Tables:
    """All of the Markdown tables to render, later.

    Attributes:
        github:
            Tables for GitHub repositories.
        unknown:
            Tables for codebases that we aren't sure whether or not they are even git
            repository.

    """

    github: dict[str, list[_GitHubRow]]
    unknown: list[_UnknownRow]

    def is_empty(self) -> bool:
        """Check if at least one table is defined."""
        return not self.github and not self.unknown


@dataclasses.dataclass(frozen=True, kw_only=True)
class _UnknownRow:
    """A codebase that we aren't sure whether or not it's a git repository."""

    url: str


def _is_github(url: str) -> bool:
    """Check if ``url`` points to a GitHub-specific repository.

    Args:
        url: A URL like ``"https://github.com/User/..."`` or ``"git@github.com:User/..."``.

    Returns:
        If ``url`` is definitely GitHub related, return ``True``.

    """
    return any(pattern.match(url) for pattern in _GITHUB_PATTERNS)


def _is_readme(name: str) -> bool:
    """Check if file ``name`` probably is documentation-related.

    Args:
        name: A path to a file on-disk. e.g. ``"/foo/bar/README.md"``.

    Returns:
        If ``name`` is documentation, return ``True``.

    """
    name = os.path.splitext(os.path.basename(name))[0]

    return name.lower() == "readme"


def _find_documentation(directory: str) -> list[str]:
    """Search ``directory`` repository for documentation-related files.

    Args:
        directory: Some git repository to look within.

    Raises:
        ValueError: If ``directory`` could not be read.

    Returns:
        All found documentation pages, if any.

    """
    if not os.path.isdir(directory):
        raise ValueError(f'Directory "{directory}" does not exist.')

    output: list[str] = []

    for name in os.listdir(directory):
        if not _is_readme(name):
            continue

        path = os.path.join(directory, name)

        with open(path, "r", encoding=_ENCODING) as handler:
            output.append(handler.read())

    return output


def _get_description_summary(details: _GitHubRepositoryDetails) -> str | None:
    """Explain ``repository`` as simply as possible.

    Args:
        details: Some git repository / codebase to load.

    Returns:
        The summary found summary, if any.

    """
    description = details["description"]

    if not description:
        return None

    if len(description) <= _DESCRIPTION_LENGTH:
        return description

    return _get_ellided_text(description, _DESCRIPTION_LENGTH)
    # TODO: Add support for this later
    # prompt = textwrap.dedent(
    #     f"""\
    #     Summarize this documentation string to {_DESCRIPTION_LENGTH} or less: {description}
    #     """
    # )
    # result = _ask_ai(prompt)
    #
    # return _get_ellided_text(result, _DESCRIPTION_LENGTH)


def _get_ellided_text(text: str, max: int) -> str:
    """Crop ``text`` to the right if it exceeds ``max``.

    Example:
        >>> _get_ellided_text("some long string", 7)
        >>> # "some..."

    Args:
        text: Raw text to crop down.
        max: The number of characters to allow.

    Returns:
        The formatted text.

    """
    if len(text) <= max:
        return text

    ellipsis = "..."

    return text[: max - len(ellipsis)] + ellipsis


def _get_first_child_of_type(
    node: tree_sitter.Node, type_name: str
) -> tree_sitter.Node:
    """Find the first child node of ``node`` that matches ``type_name``.

    Args:
        node: Some tree-sitter node to check the children of.
        type_name: Some tree-sitter language's node type.

    Raises:
        ValueError: If no match was found.

    Returns:
        The found child.

    """
    for child in node.named_children:
        if child.type == type_name:
            return child

    raise ValueError(f'Could not find "{type_name}" child in "{node}"')


def _get_github_repository_details(
    repository: _GitHubRepositoryRequest,
    headers: dict[str, str] | None = None,
) -> _GitHubRepositoryDetails:
    """Get all of the main data from some GitHub ``repository``.

    Args:
        repository: A public GitHub to query from.
        headers: HTTP request headers. Used for GitHub authentication.

    Raises:
        RuntimeError: If ``repository`` cannot be queried for data.

    Returns:
        All of the information (repository description, stars, etc).

    """
    url = f"https://api.github.com/repos/{repository.owner}/{repository.name}"
    headers = headers or {}
    headers["Accept"] = "application/vnd.github.v3+json"

    response = requests.get(url, headers=headers, timeout=5)
    response.raise_for_status()

    return typing.cast(_GitHubRepositoryDetails, response.json())


def _get_github_repository_file_tree(
    details: _GitHubRepositoryDetails,
    headers: dict[str, str] | None = None,
) -> list[_GitHubTreeResult]:
    """List the file tree using some repository ``details``.

    Args:
        details: Some git repository / codebase to load from.
        headers: HTTP request headers. Used for GitHub authentication.

    Returns:
        The found file tree. It includes nested paths.

    """
    headers = headers or {}
    url = f"https://api.github.com/repos/{details['owner']['login']}/{details['name']}/git/trees/{details['default_branch']}?recursive=1"

    response = requests.get(url, headers=headers, timeout=5)
    response.raise_for_status()

    return typing.cast(list[_GitHubTreeResult], response.json()["tree"])


def _get_github_table_rows(
    repositories: typing.Iterable[tuple[_GitHubRepositoryDetails, _GitHubRepository]],
) -> dict[str, list[_GitHubRow]]:
    """Serialize ``repositories`` to a format that is closer to raw text / markdown.

    Args:
        repositories: Some GitHub codebases to convert down.

    Returns:
        All of the Markdown table rows to consider.

    """
    output: dict[str, list[_GitHubRow]] = collections.defaultdict(list)

    for details, repository in repositories:
        description: str | None = None

        if description := _get_description_summary(details):
            description = _get_ellided_text(description, _DESCRIPTION_LENGTH)

        category = _get_primary_category(repository.documentation)
        models = _get_models(repository.documentation)

        output[category].append(
            _GitHubRow(
                description=description,
                last_commit_date=_get_last_commit_date(details),
                license=details.get("license"),
                models=models,
                name=repository.name,
                star_count=details["stargazers_count"],
                status=_get_status(repository.documentation),
                url=repository.url,
            )
        )

    return output


def _get_html_wrapper(node: tree_sitter.Node) -> _NodeWrapper:
    """Re-parse markdown ``node`` as HTML, instead.

    Args:
        node: Some markdown, parent ``tree-sitter`` node root.

    Raises:
        RuntimeError: If we cannot parse ``node`` for HTML.

    Returns:
        The resulting HTML ``tree-sitter`` wrapper.

    """
    parser = tree_sitter.Parser(_HTML_LANGUAGE)
    text = node.text

    if not text:
        raise RuntimeError(f'Node "{node}" has no text.')

    tree = parser.parse(text)

    return _NodeWrapper(tree.root_node, text)


def _get_last_commit_date(details: _GitHubRepositoryDetails) -> str:
    """Get the year, month, and day of the latest commit of some git ``details``.

    Args:
        details: Some git repository / codebase to load datetime data from.

    Returns:
        The date in the form of ``"YYYY-MM-DD"``.

    """
    # Example: raw = "2025-06-04T19:41:16Z"
    raw = details["pushed_at"]
    data = datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")

    return data.strftime("%Y-%m-%d")


def _get_models(documentation: typing.Iterable[str]) -> set[_Model]:
    """Parse ``documentation`` and look for supported AI models.

    Args:
        documentation: Some Neovim plugin's information to check.

    Returns:
        All found, supported models, if any.

    """
    output: set[_Model] = set()

    for page in documentation:
        lowered = page.lower()
        output.update(
            model
            for model in _MODELS
            if any(term for term in model.get_search_terms() if term in lowered)
        )

    return output

    # TODO: Consider using AI to find the models later
    # def _validate_results(lines: str) -> set[str]:
    #     output: set[str] = set()
    #
    #     for line in lines.split("\n"):
    #         line = line.strip()
    #
    #         if not line:
    #             continue
    #
    #         match = _BULLETPOINT_EXPRESSION.match(line)
    #
    #         if not match:
    #             raise RuntimeError(
    #                 f'line "{line}" from "{lines}" is not a bulletpoint list entry.'
    #             )
    #
    #         output.add(match.group("text").strip())
    #
    #     return output
    #
    # output: set[str] = set()
    #
    # template = textwrap.dedent(
    #     """\
    #     Here is a page of documentation for some Neovim plugin, below. It probably
    #     describes some AI features and also which models it supports.
    #
    #     ````
    #     {page}
    #     ````
    #
    #     Here is the list of AI models that you can include in the output. Absolutely do
    #     not include any text that does not match one of the following:
    #
    #     - deepseek
    #     - llama
    #     - openai
    #     """
    # )
    #
    # for page in documentation:
    #     results = _ask_ai(template.format(page=page))
    #     output.update(sorted(_validate_results(results)))
    #
    # return set(_AiModel(name=name) for name in output)


def _get_plugin_urls(lines: bytes) -> list[str] | None:
    """Find the HTTP/S URLs from some README.md ``lines``.

    Args:
        lines: Some README.md file contents to check for an existing list of plugins.

    Returns:
        All found plugins, if any.

    """

    # Example:
    #
    # <details>
    # <summary>All Plugins</summary>
    # - some URL
    # - another URL
    # </details>
    #
    # (html_block
    #   (document
    #     (element
    #       (start_tag
    #         (tag_name))
    #       (element
    #         (start_tag
    #           (tag_name))
    #         (text)
    #         (end_tag
    #           (tag_name)))
    #       (text)
    #       (end_tag
    #         (tag_name))))
    #
    def _get_plugins_text(lines: bytes) -> str | None:
        parser = tree_sitter.Parser(_MARKDOWN_LANGUAGE)
        tree = parser.parse(lines)
        all_plugins_marker = _ALL_PLUGINS_MARKER.encode()

        for node in _iter_all_nodes(tree.root_node):
            if node.type != "html_block":
                continue

            wrapper = _get_html_wrapper(node)
            outer_tag = wrapper.get(["element", "start_tag", "tag_name"])

            if outer_tag.text() != b"details":
                continue

            outer_element = wrapper.get("element")
            element = outer_element.get("element")
            tag = element.text(["start_tag", "tag_name"])

            if tag != b"summary" or element.text("text") != all_plugins_marker:
                continue

            if not node.next_sibling:
                raise RuntimeError(f'Node "{node}" has no next sibling.')

            wrapper = _NodeWrapper(node.next_sibling, lines)

            return wrapper.text("code_fence_content").decode(_ENCODING).strip()

        return None

    text = _get_plugins_text(lines)

    if not text:
        return None

    output: list[str] = []

    for line in text.split("\n"):
        line = line.strip()

        if not line or line == _MARKDOWN_FENCE_MARKER:
            continue

        match = _BULLETPOINT_EXPRESSION.match(line)

        if not match:
            raise RuntimeError(
                f'Got unexpected line "{line}", '
                f'expected to parse with "{_BULLETPOINT_EXPRESSION.pattern}" regex.',
            )

        output.append(match.group("text"))

    return output


def _get_primary_category(documentation: typing.Iterable[str]) -> str:
    return _Category.unknown

    # TODO: Add code for this later
    # categories = lest(_Category)
    #
    # for lines in documentation:
    #     result = _ask_ai("Which of these categories would you say this plugin is? Answer 0 for I don't know, 1, 2, 3 etc'", lines)
    #
    #     try:
    #         response = int(result)
    #     except TypeError:
    #         raise RuntimeError(f'Got bad "{result}" response. Expected an integer.')
    #
    #     if response:
    #         return categories[response]
    #
    # return _Category.unknown


def _get_reader_header(plugins: typing.Iterable[str]) -> str:
    """List ``plugins`` in the README markdown file.

    Args:
        plugins: All of the plugins to consider.

    Returns:
        A blob of incomplete text that represents the "top" portion of README.md

    """
    now = datetime.datetime.now()

    text = textwrap.dedent(
        f"""\
        This is a list of Neovim AI plugins.
        This page is auto-generated and was last updated on "{now.strftime('%Y-%m-%d')}"

        <details>
        <summary>{_ALL_PLUGINS_MARKER}</summary>

        ```
        {{plugins}}
        ```
        </details>
        """
    )

    return text.format(plugins="\n".join(sorted(f"- {name}" for name in plugins)))


def _get_readme_path() -> str:
    """Find the path on-disk for the input ``"README.md"`` file.

    Raises:
        EnvironmentError: If we cannot find the file.

    Returns:
        The found, absolute ``"README.md"`` path.

    """
    path = os.path.join(_CURRENT_DIRECTORY, "README.md")

    if os.path.isfile(path):
        return path

    raise EnvironmentError(f'Path "{path}" does not exist.')


def _get_status(documentation: typing.Iterable[str]) -> str | None:
    return None
    # TODO: Figure this out later
    # for lines in documentation:
    #     if ai.ask("is this plugin WIP or under construction? Reply with 0 or 1", lines) == "1":
    #         return _Status.wip
    #
    #     if ai.ask("Is this plugin mature with lots of features? Reply with 0 or 1", lines) == "1":
    #         return _Status.mature
    #
    # return _Status.none


def _get_tables_as_lines(tables: _Tables) -> list[str]:
    """Convert ``tables`` into GitHub table text.

    Args:
        tables: All serialized repository data.

    Raises:
        RuntimeError: If any of ``tables`` cannot be serialized.

    Returns:
        Each serialized table.

    """
    output: list[str] = []

    for name, rows in sorted(tables.github.items()):
        header = f"{name.capitalize()}\n{'=' * len(name)}"
        table = _serialize_github_table(rows)

        if not table:
            raise RuntimeError(f'Table "{name}" could not be serialized.')

        output.append(f"{header}\n\n{table}")

    if tables.unknown:
        name = "Unknown"
        header = f"{name}\n{'=' * len(name)}"

        raise NotImplementedError("TODO: Finish this")
        # for name, rows in sorted(tables.unknown):

    return output


def _get_table_data(plugins: typing.Iterable[str], root: str | None = None) -> _Tables:
    """Clone / Download `plugins` and read their contents.

    Args:
        plugins: Every URL, which we expect is a downloadable git repository or payload.
        root: The directory on-disk to clone to, if any.

    Returns:
        All summaries of all `plugins` to later render as a table.

    """
    unknown: list[_UnknownRow] = []

    root = root or tempfile.mkdtemp(suffix="_neovim_ai_plugin_repositories")
    repositories: list[tuple[_GitHubRepositoryDetails, _GitHubRepository]] = []

    if _GITHUB_TOKEN:
        github_headers = {"Authorization": f"token {_GITHUB_TOKEN}"}
    else:
        github_headers = {}

    seen: set[str] = set()

    for url in plugins:
        if not _is_github(url):
            unknown.append(_UnknownRow(url=url))

            continue

        request = _GitHubRepositoryRequest.from_url(url)
        details = _get_github_repository_details(request, headers=github_headers)

        if details["html_url"] in seen:
            continue

        seen.add(details["html_url"])

        repository = _download_github_files(details, root, headers=github_headers)
        repositories.append((details, repository))

    github = _get_github_table_rows(repositories)

    return _Tables(github=github, unknown=unknown)


def _iter_all_nodes(
    node: tree_sitter.Node,
) -> typing.Generator[tree_sitter.Node, None, None]:
    """Get all children from ``node``, recursively.

    Important:
        This function is **inclusive**. ``node`` is always included in the result.

    Args:
        node: Some tree-sitter node to walk down

    Yields:
        All found children.

    """
    stack = [node]

    while stack:
        node = stack.pop()

        yield node

        stack.extend(reversed(node.children))


def _ask_ai(prompt: str) -> str:
    """Ask an chatbot AI ``prompt`` and get its raw response back..

    Args:
        prompt: Some input to send to the AI. Maybe "summarize this page" or something.

    Returns:
        The AI's response.

    """
    raise RuntimeError(prompt)


def _download_github_documentation_files(
    details: _GitHubRepositoryDetails,
    base_url: str,
    directory: str,
    headers: dict[str, str] | None = None,
) -> None:
    """Search the repository ``details`` for documentation (README) files.

    Args:
        details: Some git repository / codebase to load from.
        base_url: A GitHub branch URL. e.g. ``"https://github.com/foo/bar/main"``.
        directory: A directory to clone into.
        headers: HTTP request headers. Used for GitHub authentication.

    """
    headers = headers or {}
    os.makedirs(directory, exist_ok=True)

    for item in _get_github_repository_file_tree(details, headers=headers):
        if item["type"] != "blob":
            continue

        if not _is_readme(item["path"]):
            continue

        url = f"{base_url}/{item['path']}"
        path = os.path.join(directory, os.path.basename(item["path"]))

        _LOGGER.info('Downloading "%s" path.', item["path"])

        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()

        with open(path, "wb") as handler:
            handler.write(response.content)


def _download_github_files(
    details: _GitHubRepositoryDetails,
    directory: str,
    headers: dict[str, str] | None = None,
) -> _GitHubRepository:
    """Clone the git ``url`` to ``directory``.

    Args:
        details: Some git repository / codebase to download.
        directory: A directory to clone into.
        headers: HTTP request headers. Used for GitHub authentication.

    Returns:
        A summary of the cloned git repository.

    """
    headers = headers or {}
    directory = os.path.join(directory, details["owner"]["login"], details["name"])

    if not os.path.isdir(directory):
        url = f"https://raw.githubusercontent.com/{details['owner']['login']}/{details['name']}/{details['default_branch']}"
        _LOGGER.info('Cloning "%s" repository to "%s" directory.', url, directory)
        _download_github_documentation_files(details, url, directory, headers=headers)
    else:
        _LOGGER.info(
            'Skipped cloning "%s" repository to "%s" directory. It already exists.',
            details["html_url"],
            directory,
        )

    return _GitHubRepository(
        directory=directory,
        documentation=_find_documentation(directory),
        name=details["name"],
        owner=details["owner"]["login"],
        url=details["html_url"],
    )


def _generate_readme_text(path: str, root: str | None = None) -> str:
    """Read ``path`` and regenerate its contents.

    Args:
        path: Some ``"/path/to/README.md"`` to make again.
        root: The directory on-disk to clone repositories to, if any.

    Raises:
        RuntimeError: If no ``plugins`` to generate were found.

    Returns:
        The full ``"README.md"`` text.

    """
    with open(path, "rb") as handler:
        data = handler.read()

    plugins = sorted(_get_plugin_urls(data) or [])

    if not plugins:
        raise RuntimeError(f'Path "{path}" has no parseable plugins list.')

    header = _get_reader_header(plugins)
    table_data = _get_table_data(plugins, root)

    if table_data.is_empty():
        middle = ""
    else:
        tables = _get_tables_as_lines(table_data)
        middle = "\n\n" + "\n".join(tables)

    return (
        header
        + middle
        + textwrap.dedent(
            """


            ## Generating This List
            ```sh
            GITHUB_TOKEN="your API token here" make generate
            # Or directly
            GITHUB_TOKEN="your API token here" python generate_readme.md --directory /tmp/repositories
            ```
            """
        )
    )


def _git(command: str, directory: str | None = None) -> str:
    """Run the ``git`` command. e.g. ``"clone <some URL>"``.

    Args:
        command: The git command to run (don't include the ``git`` executable prefix).
        directory: The directory to run the command from, if any.

    Raises:
        RuntimeError: If the git commant fails.

    Returns:
        The raw terminal output of the command.

    """
    process = subprocess.Popen(
        ["git", *shlex.split(command)],
        cwd=directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    stdout, stderr = process.communicate()

    if process.returncode:
        raise RuntimeError(f"Got error during git call:\n{stderr}")

    return stdout


def _parse_arguments(text: typing.Sequence[str]) -> _ParsedArguments:
    """Convert raw user CLI text into actual Python objects.

    Args:
        text: Terminal / CLI user input. e.g. ``["--directory", "/tmp/repositories"]``.

    Returns:
        The parsed settings.

    """
    parser = argparse.ArgumentParser(
        description="Options to affect the README.md generator."
    )
    parser.add_argument(
        "--directory",
        default=tempfile.mkdtemp(suffix="_neovim_ai_plugins"),
        help="The path on-disk to clone temporary github repositories into.",
    )

    namespace = parser.parse_args(text)

    return _ParsedArguments(directory=namespace.directory)


def _serialize_github_table(rows: typing.Iterable[_GitHubRow]) -> str | None:
    """Write a GitHub table containing ``rows``.

    Args:
        rows: Each line of data to show.

    Returns:
        The full table, including the header and body.

    """
    if not rows:
        return None

    tables: list[str] = []

    for row in rows:
        models = (
            " ".join(sorted(model.serialize_to_markdown_tag() for model in row.models))
            or "<No AI models were found>"
        )

        license = "`<No license found>`"

        if row.license:
            license = _get_license_as_markdown(row.license)

        parts = [
            row.get_repository_label(),
            row.description or "`<No description found>`",
            f":star2: {row.star_count}",
            models,
            row.last_commit_date,
            license,
        ]
        tables.append(f"| {' | '.join(parts)} |")

    header = [
        "| :ab: Name | :notebook: Description | :star2: Stars | :robot: Models | :date: Updated | :balance_scale: License |",
        "| --------- | ---------------------- | ------------- | -------------- | -------------- | ----------------------- |",
    ]

    return "\n".join(itertools.chain(header, sorted(tables)))


def _get_license_as_markdown(license: _GitHubRepositoryDetailsLicense) -> str:
    """Convert ``license`` to text that markdown can render.

    Args:
        license: Some raw GitHub license information. e.g. name, URL, ID, etc.

    Returns:
        The markdown to render. Usually a name and a link to learn more.

    """
    name = license["name"]
    # NOTE: It's redundant for every row to say "Foo License" over and over again so we
    # might as well remove it.
    #
    name = name.replace(" License", "")

    if not license["url"]:
        return name

    return f"[{name}]({license['url']})"


def _validate_environment() -> None:
    """Make sure this scdripting environment has what it needs to run successfully.

    Raises:
        EnvironmentError: If we're missing a ``git`` CLI.

    """
    if not shutil.which("git"):
        raise EnvironmentError("No git executable waas found.")


def _verify(value: T | None) -> T:
    """Make sure ``value`` exists.

    Args:
        value: Some value (or empty value).

    Raises:
        RuntimeError: If ``value`` is not defined.

    Returns:
        The original value.

    """
    if value is not None:
        return value

    raise RuntimeError("Expected a value but found None.")


def _main(text: typing.Sequence[str]) -> None:
    """Generate the README.md.

    Args:
        text: Raw CLI user input to parse.

    """
    _validate_environment()
    namespace = _parse_arguments(text)

    path = _get_readme_path()
    data = _generate_readme_text(path, root=namespace.directory)

    _LOGGER.info("Generated README.md data\n\n````%s````", data)

    with open(path, "w", encoding=_ENCODING) as handler:
        handler.write(data)


if __name__ == "__main__":
    _HANDLER = logging.StreamHandler(sys.stdout)
    _HANDLER.setLevel(logging.INFO)
    _FORMATTER = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    _HANDLER.setFormatter(_FORMATTER)
    _LOGGER.addHandler(_HANDLER)
    _LOGGER.setLevel(logging.INFO)

    _main(sys.argv[1:])

# TODO: Finish this
# - Get list of plugins
# - Clone / download them all
# - read their README(s) to get an overview of what they do
#  - Fallback: read their GitHub description, if it's from GitHub
#  - Fallback: add "<No description found>"
# - Ellide the description if it's too long
#
# - Use an AI or something to guess which group a plugin primarily belongs to.
#  - code writing and editing
#  - conversation-focused
#  - tab completion
#  - other
#
# - Write header
#  - Explain this repository + also add the latest datetime generated
#
# e.g. data from - https://github.com/jkitching/awesome-vim-llm-plugins
# - star count
# - tags. e.g. #inline #model:foo
# - status (WIP or not)
#
# Using the data above, generate the README.md again. Probably use tree-sitter + parso
#
# Make a GitHub action runner. Runs once a week

from __future__ import annotations

from urllib import parse
import collections
import dataclasses
import datetime
import enum
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import typing

import requests
import tree_sitter
import tree_sitter_markdown


_ALL_PLUGINS_MARKER = "All Plugins"
_BULLETPOINT_EXPRESSION = re.compile("^\s*-\s*(?P<model>.+)$")
_CURRENT_DIRECTORY = os.path.dirname(os.path.realpath(__file__))
_ENCODING = "utf-8"
_LANGUAGE = tree_sitter.Language(tree_sitter_markdown.language())
_OKAY_HTTP_STATUS = 200
_GITHUB_PATTERNS = (
    re.compile(r"^https?://github\.com/"),
    re.compile(r"^git@github\.com:"),
    re.compile(r"^git://github\.com/"),
)
T = typing.TypeVar("T")


class _AiModel:
    """Some AI model name, e.g. ``"openai"``, to display differently, later."""

    def __init__(self, name: str) -> None:
        """Keep track of an AI model name.

        Args:
            name: e.g. ``"openai"``, ``"deepseek"``, etc.

        """
        self._name = name

    def serialize_to_markdown_tag(self) -> str:
        """Get the "pretty markdown" version of this model name."""
        return f"`#model:{self._name}`"


class _PrimaryCategory(str, enum.Enum):
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
            RuntimeError: [TODO:throw]

        Returns:
            [TODO:return]
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


@dataclasses.dataclass(frozen=True, kw_only=True)
class _GitHubRow:
    """The data to serialize into GitHub markdown row text, later."""

    description: str
    last_commit_date: str
    models: set[_AiModel]
    name: str
    star_count: str
    status: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class _Repository:
    """A small abstraction over a git repository."""

    directory: str
    documentation: list[str]
    name: str
    owner: str


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

    Returns:
        All found documentation pages, if any.

    """
    output: list[str] = []

    for name in os.listdir(directory):
        if not _is_readme(name):
            continue

        path = os.path.join(directory, name)

        with open(path, "r", encoding=_ENCODING) as handler:
            output.append(handler.read())

    return output


def _get_description(repository: _Repository) -> str:
    raise NotImplementedError("TODO")


def _get_description_summary(repository: _Repository) -> str | None:
    """Explain ``repository`` as simply as possible.

    Args:
        repository: Some git repository / codebase to load.

    Returns:
        The summary found summary, if any.

    """
    description = _get_description(repository)

    if not description:
        return None

    length = 120

    if len(description) <= length:
        return description

    prompt = textwrap.dedent(
        f"""\
        Summarize this documentation string to {length} or less: {description}
        """
    )
    result = _ask_ai(prompt)

    return _get_ellided_text(result, length)


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


def _get_last_commit_date(directory: str) -> str:
    """Get the year, month, and day of the latest commit of some git ``directory``.

    Args:
        directory: An absolute path on-disk to some git repository.

    Returns:
        The date in the form of ``"YYYY-MM-DD"``.

    """
    return _git("show -s --format=%cd --date=format:'%Y-%m-%d' HEAD", directory)


def _get_models(documentation: typing.Iterable[str]) -> set[_AiModel]:
    """Parse ``documentation`` and look for supported AI models.

    Args:
        documentation: Some Neovim plugin's information to check.

    Returns:
        All found, supported models, if any.

    """

    def _validate_results(lines: str) -> set[str]:
        output: set[str] = set()

        for line in lines.split("\n"):
            line = line.strip()

            if not line:
                continue

            match = _BULLETPOINT_EXPRESSION.match(line)

            if not match:
                raise RuntimeError(
                    f'line "{line}" from "{lines}" is not a bulletpoint list entry.'
                )

            output.add(match.group("model").strip())

        return output

    output: set[str] = set()

    template = textwrap.dedent(
        """\
        Here is a page of documentation for some Neovim plugin, below. It probably
        describes some AI features and also which models it supports.

        ````
        {page}
        ````

        Please output a bulletpoint list of models that this page supports. For example:

        - deepseek
        - llama
        - openai

        Output just the bulletpoint list. Do not say anything else.
        """
    )

    for page in documentation:
        results = _ask_ai(template.format(page=page))
        output.update(sorted(_validate_results(results)))

    return set(_AiModel(name=name) for name in output)


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
    parser = tree_sitter.Parser(_LANGUAGE)
    tree = parser.parse(lines)
    all_plugins_marker = _ALL_PLUGINS_MARKER.encode()

    for node in _iter_all_nodes(tree.root_node):
        if node.type != "html_block":
            continue

        wrapper = _NodeWrapper(node, lines)
        element = wrapper.get(["document", "element"])
        tag = element.get(["start_tag", "tag_name"])

        if (
            tag.text() == b"summary"
            and element.text(["element", "text"]) == all_plugins_marker
        ):
            text = tag.get("text").text().decode(_ENCODING)

            return text.split("\n")

    return None


def _get_primary_category(documentation: typing.Iterable[str]) -> str:
    raise NotImplementedError("TODO: finish")

    # categories = list(_PrimaryCategory)
    #
    # for lines in documentation:
    #     result = ai.ask("Which of these categories would you say this plugin is? Answer 0 for I don't know, 1, 2, 3 etc'", lines)
    #
    #     try:
    #         response = int(result)
    #     except TypeError:
    #         raise RuntimeError(f'Got bad "{result}" response. Expected an integer.')
    #
    #     if response:
    #         return categories[response]
    #
    # return _PrimaryCategory.unknown


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
        {{plugins}}
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


def _get_star_count(repository: _Repository) -> str:
    """Get the GitHub star count from ``repository``.

    Args:
        repository: Each GitHub URL.

    Raises:
        RuntimeError: If ``repository`` cannot be queried for stars.

    Returns:
        The found, 0-or-more star-count.

    """
    url = f"https://api.github.com/repos/{repository.owner}/{repository.name}"
    headers = {"Accept": "application/vnd.github.v3+json"}
    response = requests.get(url, headers=headers)
    code = response.status_code

    if code != _OKAY_HTTP_STATUS:
        raise RuntimeError(
            f'Unable to get a star count for "{repository}". Got "{code}" code.',
        )

    data = response.json()
    count = data["stargazers_count"]

    if isinstance(count, int):
        return str(count)

    raise RuntimeError(f'Got unknown "{count}" from "{repository}" repository.')


def _get_status(documentation: typing.Iterable[str]) -> str:
    raise NotImplementedError("TODO: finish")

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
        header = f"{name}\n{'=' * len(name)}"
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
    github: dict[str, list[_GitHubRow]] = collections.defaultdict(list)
    unknown: list[_UnknownRow] = []

    root = root or tempfile.mkdtemp(suffix="_neovim_ai_plugin_repositories")

    for url in plugins:
        if not _is_github(url):
            unknown.append(_UnknownRow(url=url))

            continue

        repository = _download(url, root)
        directory = repository.directory
        description = _get_description_summary(repository) or "<No description found>"
        description = _get_ellided_text(description, 120)
        category = _get_primary_category(repository.documentation)
        models = _get_models(repository.documentation)

        github[category].append(
            _GitHubRow(
                description=description,
                last_commit_date=_get_last_commit_date(directory),
                models=models,
                name=repository.name,
                star_count=_get_star_count(repository),
                status=_get_status(repository.documentation),
            )
        )

    return _Tables(github=github, unknown=unknown)


def _ask_ai(prompt: str) -> str:
    """Ask an chatbot AI ``prompt`` and get its raw response back..

    Args:
        prompt: Some input to send to the AI. Maybe "summarize this page" or something.

    Returns:
        The AI's response.

    """
    raise RuntimeError(prompt)


def _download(url: str, directory: str) -> _Repository:
    """Clone the git ``url`` to ``directory``.

    Args:
        url: The HTTP / HTTPS / SSH git repository to clone.
        directory: A directory to clone into.

    Returns:
        A summary of the cloned git repository.

    """
    parsed = parse.urlparse(url)
    parts = parsed.path.split("/")
    owner = parts[-2]
    name = parts[-1]
    directory = os.path.join(directory, name)

    _git(f"clone {url} {directory}")

    return _Repository(
        directory=directory,
        documentation=_find_documentation(directory),
        name=name,
        owner=owner,
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
    tables = _get_tables_as_lines(_get_table_data(plugins, root))

    return header + "\n".join(tables)


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
        f"git {command}",
        cwd=directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    stdout, stderr = process.communicate()

    if process.returncode:
        raise RuntimeError(f"Got error during git call:\n{stderr}")

    return stdout


def _serialize_github_table(rows: typing.Iterable[_GitHubRow]) -> str | None:
    """Write a GitHub table containing ``rows``.

    Args:
        rows: Each line of data to show.

    Returns:
        The full table, including the header and body.

    """
    if not rows:
        return None

    output = [
        "| Name | Description | Star Count | Models | Status | Last Commit Date |",
        "| ---- | ----------- | ---------- | ------ | ------ | ---------------- |",
    ]

    for row in rows:
        models = (
            " ".join(sorted(model.serialize_to_markdown_tag() for model in row.models))
            or "<No AI models were found>"
        )
        parts = [
            row.name,
            row.description,
            row.star_count,
            row.status,
            models,
            row.last_commit_date,
        ]
        output.append(f"| {' | '.join(parts)} |")

    return "\n".join(output)


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


def main() -> None:
    """Generate the README.md."""
    _validate_environment()

    path = _get_readme_path()
    data = _generate_readme_text(path)

    with open(path, "w", encoding=_ENCODING) as handler:
        handler.write(data)


if __name__ == "__main__":
    main()

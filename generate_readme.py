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
import enum
import collections
import dataclasses
import datetime
import os
import subprocess
import tempfile
import textwrap
import typing

import requests
import tree_sitter
import tree_sitter_markdown


_ALL_PLUGINS_MARKER = "All Plugins"
_CURRENT_DIRECTORY = os.path.dirname(os.path.realpath(__file__))
_ENCODING = "utf-8"
_LANGUAGE = tree_sitter.Language(tree_sitter_markdown.language())
_OKAY_HTTP_STATUS = 200
T = typing.TypeVar("T")


class _PrimaryCategory(str, enum.Enum):
    code_editting = "code-editting"
    auto_completion = "auto-completion"
    communication = "communication / chat"
    unknown = "unknown"


class _NodeWrapper:
    def __init__(self, node: tree_sitter.Node, data: bytes) -> None:
        super().__init__()

        self._node = node
        self._data = data

    def get(self, path: typing.Iterable[str | int] | str | int) -> _NodeWrapper:
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

    def text(self, path: typing.Iterable[str] | str | None=None) -> bytes:
        if path:
            node = self.get(path)

            return node.text()

        return self._data[self._node.start_byte:self._node.end_byte]


@dataclasses.dataclass(frozen=True, kw_only=True)
class _GitHubRow:
    cost: str
    description: str
    last_commit_date: str
    name: str
    star_count: str
    status: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class _Repository:
    directory: str
    documentation: list[str]
    name: str
    owner: str


class _Status(str, enum.Enum):
    wip = "wip"
    mature = "mature"


@dataclasses.dataclass(frozen=True, kw_only=True)
class _Tables:
    github: dict[str, list[_GitHubRow]]
    unknown: list[_UnknownRow]


def _is_readme(name: str) -> bool:
    return os.path.basename(name).lower() == "readme"


def _get_description(repository: _Repository) -> str:
    raise ValueError("ASD")


def _get_documentation(directory: str) -> list[str]:
    output: list[str] = []

    for name in os.listdir(directory):
        if not _is_readme(name):
            continue

        path = os.path.join(directory, name)

        with open(path, "r", encoding=_ENCODING) as handler:
            output.append(handler.read())

    return output


def _get_ellided_text(text: str, max: int) -> str:
    if len(text) <= max:
        return text

    ellipsis = "..."

    return text[:max - len(ellipsis)] + ellipsis


def _get_first_child_of_type(node: tree_sitter.Node, type_name: str) -> tree_sitter.Node:
    for child in node.named_children:
        if child.type == type_name:
            return child

    raise RuntimeError(f'Could not find "{type_name}" child in "{node}"')


def _get_header_block(plugins: typing.Iterable[str]) -> str:
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

    return text.format(
        plugins="\n".join(sorted(f"- {name}" for name in plugins))
    )


def _get_last_commit_date(directory: str) -> str:
    return _git("show -s --format=%cd --date=format:'%Y-%m-%d' HEAD", directory)


def _get_plugin_urls(lines: bytes) -> list[str] | None:
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


def _get_readme_path() -> str:
    path = os.path.join(_CURRENT_DIRECTORY, "README.md")

    if os.path.isfile(path):
        return path

    raise EnvironmentError(f'Path "{path}" does not exist.')


def _get_star_count(repository: _Repository) -> str:
    url = f"https://api.github.com/repos/{repository.owner}/{repository.name}"
    headers = {'Accept': 'application/vnd.github.v3+json'}
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


def _get_table_blocks(tables: _Tables) -> list[str]:
    output: list[str] = []

    for name, rows in sorted(tables.github):
        header = f"{name}\n{'=' * len(name)}"
        table = _serialize_table_text(rows)

        if not table:
            raise RuntimeError(f'Table "{name}" could not be serialized.')

        output.append(f"{header}\n\n{table}")

    if tables.unknown:
        name = "Unknown"
        header = f"{name}\n{'=' * len(name)}"

        raise NotImplementedError("TODO: Finish this")
        # for name, rows in sorted(tables.unknown):

    return output


def _get_table_data(plugins: typing.Iterable[str], root: str | None=None) -> _Tables:
    github: dict[str, list[_GitHubRow]] = collections.defaultdict(list)
    unknown: list[_UnknownRow] = []

    root = root or tempfile.mkdtemp(suffix="_neovim_ai_plugin_repositories")

    for url in plugins:
        if not _is_github(url):
            unknown.append(_UnknownRow(url=url))

            continue

        repository = _download(url, root)
        directory = repository.directory
        description = _get_description(repository) or "<No description found>"
        description = _get_ellided_text(description, 120)
        category = _get_primary_category(repository.documentation)

        github[category].append(
            _GitHubRow(
                description=description,
                last_commit_date=_get_last_commit_date(directory),
                name=repository.name,
                star_count=_get_star_count(repository) or "N/A",
                status=_get_status(repository.documentation),
            )
        )

    return _Tables(github=github, unknown=unknown)


def _download(url: str, directory: str="") -> _Repository:
    parsed = parse.urlparse(url)
    parts = parsed.path.split("/")
    owner = parts[-2]
    name = parts[-1]
    directory = os.path.join(directory, name)

    _git(f'clone {url} {directory}')

    return _Repository(
        directory=directory,
        documentation=_get_documentation(directory),
        name=name,
        owner=owner,
    )


def _generate_readme_text(path: str, root: str | None=None) -> str:
    with open(path, "rb") as handler:
        data = handler.read()

    plugins = sorted(_get_plugin_urls(data))

    if not plugins:
        raise RuntimeError(f'Path "{path}" has no parseable plugins list.')

    tables = _get_table_data(plugins, root)
    header_block = _get_header_block(plugins)
    table_blocks = _get_table_blocks(tables)

    return header_block + "\n".join(table_blocks)


def _git(command: str, directory: str | None=None) -> str:
    process = subprocess.Popen(
        f"git {command}",
        cwd=directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    stdout, stderr = process.communicate()

    if process.returncode:
        raise RuntimeError(f'Got error during git call:\n{stderr}')

    return stdout


def _serialize_table_text(rows: typing.Iterable[_GitHubRow]) -> str:
    if not rows:
        return ""

    output = [
        "| Name | Description | Star Count | Status | Last Commit Date |",
        "| ---- | ----------- | ---------- | ------ | ---------------- |",
    ]

    for row in rows:
        parts = [
            row.name,
            row.description,
            row.star_count,
            row.status,
            row.last_commit_date,
        ]
        output.append(f"| {' | '.join(parts)} |")

    return "\n".join(output)


def _validate_environment() -> None:
    if not shutil.which("git"):
        raise EnvironmentError('No git executable waas found.')


def _verify(value: T | None) -> T:
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

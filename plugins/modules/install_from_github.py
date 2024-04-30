#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import re
import urllib.request
from ansible.module_utils.basic import AnsibleModule
import json
import tempfile
import shutil
import os
from typing import Union, List
import bz2
import gzip
import lzma
import platform
import filecmp
from collections import defaultdict


DOCUMENTATION = r"""
module: install_from_github
short_description: Download and install assets from Github releases page.
description:
  - This module can be used to select a release from a Github repository,
    select an asset from that release based on OS and CPU architecture,
    download the asset, and install files/directories from the asset.
author:
  - Mohammad Javad Naderi
options:
  repo:
    description:
      - The name of the repository in the format `user_or_org/repo_name`.
    required: true
    type: str

  tag:
    description:
      - The tag to select from releases page.
        The default (`latest`) means the most recent non-prerelease, non-draft release.
    default: latest
    type: str

  asset_regex:
    description:
      - A regex for selecting an asset (file name) from all the assets of selected release.
        If there are multiple assets for different OSes and CPU architectures, you don't need
        to specify OS (darwin, linux, ...) and architecture (x86_64, amd64, aarch64, arm64, ...)
        in your regex (just write `.*` in place of them). This module tries to narrow down
        assets based on the system's OS and CPU architecture.
    required: true
    type: str

  asset_arch_mapping:
    description:
      - 'If the repo uses non-standard strings to specify CPU architecture, you can define a custom
        mapping between those and standard architectures. For example, if some repo uses `64` instead
        of `x86_64` or `amd64`, you can set this option to `amd64: "64"` or `x86_64: "64"`.'
    required: false
    type: dict

  version_command:
    description:
      - The command to get the currently installed version. (e.g. `some_command --version`)
        If the output of this command matches the selected asset, downloading and installing
        the asset is skipped.
    required: false
    type: str

  version_regex:
    description:
      - A regex for extracting version from the output of `version_command` or tag name.
        The default is to match 2 or 3 numbers joined by `.`. E.g. 1.12.7 or 1.12
    required: false
    type: str
    default: \d+\.\d+(?:\.\d+)?

  version_file:
    description:
      - Path to a file containing the version of currently installed version. The module
        reads the version from this file instead of `version_command` before installing
        (to skip download if the desired version is installed) and writes the installed
        version to this file after successful installation.
        This is useful for non-executable assets which don't have any `--version` command
        (e.g. fonts, ...).
        If you pass `version_file`, you can't pass `version_command` or `version_regex` options.
    required: false
    type: path

  move_rules:
    description:
      - You need to specify how individual items from an asset should be moved to the system.
        Privide a list of rules. Each rule should specify `src_regex` and `dst`, and could specify
        `mode`, `owner`, `group`.
        An asset can be a single file, or an archive (`.zip`, `.tar.gz`, ...). When asset is an archive,
        you select by `src_regex` some paths (directories or files) relative to archive root, and they
        will move to `dst`. Even if the asset is just a single file (not an archive), you should specify
        a rule to move that file (`src_regex` can be any regex mathing file name, e.g. `.*`).
    required: true
    type: list
"""

RETURN = ""

EXAMPLES = r"""
- name: install latest version of lego (ACME client)
  quera.github.install_from_github:
    repo: go-acme/lego
    asset_regex: lego.*\.tar\.gz
    version_command: lego --version
    move_rules:
      - src_regex: lego
        dst: /usr/local/bin
        mode: 0755

- name: install a specific version of sentry-cli
  quera.github.install_from_github:
    repo: getsentry/sentry-cli
    tag: "2.8.1"
    asset_regex: sentry-cli-.*
    version_command: sentry-cli --version
    move_rules:
      - src_regex: sentry-cli-.*
        dst: /usr/local/bin/sentry-cli
        mode: 0755

- name: install both executable and data
  quera.github.install_from_github:
    repo: example/example
    asset_regex: example-.*\.zip
    asset_arch_mapping:
      amd64: "64"  # This repo indicates amd64 as example-64.zip instead of example-amd64.zip or example-x86_64.zip.
    version_command: example --version
    move_rules:
      - src_regex: example
        dst: /usr/local/bin
        mode: 0755
      - src_regex: .*\.dat
        dst: /usr/local/share/example
        mode: 0644

- name: install some data file (e.g. font, ...)
  quera.github.install_from_github:
    repo: example/example
    asset_regex: exampledata.dat
    version_file: /usr/local/share/example/exampledata.dat.version
    move_rules:
      - src_regex: exampledata.dat
        dst: /usr/local/share/example
"""


def get_json_url(url: str) -> dict:
    return json.load(urllib.request.urlopen(url))


def files_have_same_content(path1: str, path2: str):
    if not os.path.isfile(path1) or not os.path.isfile(path2):
        raise Exception
    return filecmp.cmp(path1, path2)


def extract_version(s: str, version_regex: str) -> Union[str, None]:
    m = re.search(version_regex, s)
    if m:
        return m.group(0)


def is_download_required(
    module: AnsibleModule,
    version_command: str,
    version_regex: str,
    version_file: str,
    release_info: dict,
):
    if version_file:
        if not os.path.exists(version_file):
            return True
        with open(version_file, "r") as fp:
            version_file_content = fp.read().strip()
        return version_file_content != release_info["tag_name"]
    if not version_command:
        return True
    try:
        rc, result_stdout, _ = module.run_command(
            version_command, handle_exceptions=False
        )
    except (OSError, IOError):
        return True
    else:
        version_installed = extract_version(result_stdout.strip(), version_regex)
        if not version_installed:
            module.fail_json(
                msg='The output of "version_command" did not contain a version.',
            )
        version_to_install = extract_version(release_info["tag_name"], version_regex)
        if not version_to_install:
            module.fail_json(
                msg="The tag name of Github release does not contain a version.",
            )
        return version_installed != version_to_install


def decompress_file(path: str):
    path0, ext = os.path.splitext(path)
    if ext in [".bz2", ".bz", ".bzip"]:
        CompressedFile = bz2.BZ2File
    elif ext in [".xz", ".lzma"]:
        CompressedFile = lzma.LZMAFile
    elif ext in [".gz"]:
        CompressedFile = gzip.GzipFile
    else:
        return path
    with CompressedFile(path) as fr, open(path0, "wb") as fw:
        shutil.copyfileobj(fr, fw)
    return path0


def move_paths(module: AnsibleModule, paths_to_move: dict, validate_only=False) -> bool:
    if not validate_only:
        # We need this to be atomic (move all or nothing).
        # So we validate first, and then move files.
        move_paths(module, paths_to_move, validate_only=True)
    changed = False
    for dest, path_list in paths_to_move.items():
        if os.path.isdir(dest):
            for p in path_list:
                dst_path = os.path.join(dest, os.path.basename(p))
                if os.path.isdir(p) and os.path.exists(dst_path):
                    module.fail_json(
                        msg=f"Destination path '{dst_path}' already exists."
                    )
            if not validate_only:
                for abs_path in path_list:
                    shutil.move(
                        abs_path, os.path.join(dest, os.path.basename(abs_path))
                    )
                changed = True
        else:
            if len(path_list) > 1:
                module.fail_json(msg=f"Can't move multiple files/dirs to '{dest}'.")
            abs_path = path_list[0]
            if not os.path.exists(dest):
                if not os.path.exists(os.path.dirname(dest)):
                    module.fail_json(
                        msg=f"Directory '{os.path.dirname(dest)}' does not exist."
                    )
                if not validate_only:
                    shutil.move(abs_path, dest)
                    changed = True
            else:
                if os.path.isdir(abs_path):
                    module.fail_json(msg=f"File '{dest}' exists.")
                if not validate_only:
                    if not files_have_same_content(abs_path, dest):
                        changed = True
                    shutil.move(abs_path, dest)
    return changed


def set_mode_owner_group(module: AnsibleModule, path: str, mode, owner, group):
    module.set_owner_if_different(path, owner or os.getuid(), False)
    module.set_group_if_different(path, group or os.getgid(), False)
    if mode is not None:
        module.set_mode_if_different(path, mode, False)
    if os.path.isdir(path):
        for item in os.listdir(path):
            set_mode_owner_group(module, os.path.join(path, item), mode, owner, group)


def download_asset(module: AnsibleModule, file_name: str, url: str, move_rules: dict):
    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = os.path.join(temp_dir, file_name)
        urllib.request.urlretrieve(url, file_path)

        file_path = decompress_file(file_path)

        extract_dir = os.path.join(temp_dir, "extract")
        os.mkdir(extract_dir)

        try:
            # try extracting if downloaded file is an archive
            shutil.unpack_archive(file_path, extract_dir)
        except shutil.ReadError:
            shutil.move(file_path, extract_dir)

        paths_to_move = defaultdict(list)
        paths_to_move_rule = {}

        for root, dirs, files in os.walk(extract_dir):
            for item in dirs + files:
                abs_path = os.path.join(root, item)
                already_included = any(
                    os.path.commonpath([p, abs_path]) == p
                    for path_list in paths_to_move.values()
                    for p in path_list
                )
                if already_included:
                    continue
                for move_rule in move_rules:
                    if re.fullmatch(
                        move_rule["src_regex"], os.path.relpath(abs_path, extract_dir)
                    ):
                        paths_to_move[move_rule["dst"]].append(abs_path)
                        paths_to_move_rule[abs_path] = move_rule
                        break

        for p, move_rule in paths_to_move_rule.items():
            set_mode_owner_group(
                module,
                p,
                move_rule.get("mode"),
                move_rule.get("owner"),
                move_rule.get("group"),
            )

        return move_paths(module, paths_to_move)


class AssetSelector:
    asset_regex: re.Pattern
    system: str
    architectures: List[str]

    class AssetSelectionFailed(Exception):
        pass

    def __init__(self,  asset_regex: re.Pattern, asset_arch_mapping: dict):
        self.asset_regex = asset_regex
        self.system = platform.system().lower()  # linux, darwin, windows, ...
        machine = platform.machine().lower()
        architectures: List[str] = {
            "x86_64": ["x86_64", "amd64"],
            "amd64": ["x86_64", "amd64"],
            "aarch64": ["aarch64", "arm64"],
            "arm64": ["aarch64", "arm64"],
        }.get(machine, [machine])
        try:
            arch_mapping_key = list(set(architectures).intersection(asset_arch_mapping.keys()))[0]
        except IndexError:
            self.architectures = architectures
        else:
            self.architectures = (
                asset_arch_mapping[arch_mapping_key]
                if type(asset_arch_mapping[arch_mapping_key]) == list
                else [asset_arch_mapping[arch_mapping_key]]
            )

    def asset_matches_system(self, asset: dict) -> bool:
        return self.system in asset["name"].lower()

    def asset_matches_architecture(self, asset: dict) -> bool:
        return any(
            re.search(rf"(?:^|\W|_){re.escape(architecture)}(?:$|\W|_)", asset["name"].lower())
            for architecture in self.architectures
        )

    def select_asset(
        self,
        assets: List[dict]
    ) -> dict:
        filtered_assets = [asset for asset in assets if self.asset_regex.fullmatch(asset["name"])]
        if len(filtered_assets) == 0:
            raise self.AssetSelectionFailed('No asset matched "asset_regex".')
        if len(filtered_assets) == 1:
            return assets[0]

        # try filtering assets based on architecture
        filtered_assets = [asset for asset in filtered_assets if self.asset_matches_architecture(asset)]
        if len(filtered_assets) == 0:
            raise self.AssetSelectionFailed(
                'More than one asset matched "asset_regex". '
                f'Tried to filter them based on architecture ({",".join(self.architectures)}), but no asset matched.'
            )
        if len(filtered_assets) == 1:
            return filtered_assets[0]

        # try filtering assets based on system
        filtered_assets = [asset for asset in filtered_assets if self.asset_matches_system(asset)]
        if len(filtered_assets) == 0:
            raise self.AssetSelectionFailed(
                f'More than one asset matched "asset_regex" and architecture ({",".join(self.architectures)}). '
                f'Tried to filter them based on system ({self.system}), but no asset matched.'
            )
        if len(filtered_assets) == 1:
            return filtered_assets[0]

        raise self.AssetSelectionFailed(f"Couldn't select a unique asset. Assets matched: {len(filtered_assets)}")


def main():
    module = AnsibleModule(
        argument_spec={
            # 1. select repo
            "repo": {"required": True, "type": "str"},
            # 2. select release
            #   if tag is not provided, we get the latest release (the most recent non-prerelease, non-draft release)
            "tag": {"required": False, "type": "str", "default": "latest"},
            # 3. select asset
            "asset_regex": {"required": True, "type": "str"},
            "asset_arch_mapping": {"required": False, "type": "dict", "default": {}},
            # 4. (optional) check installed version (to see if download is required)
            "version_command": {"required": False, "type": "str"},
            "version_regex": {"required": False, "type": "str"},
            "version_file": {"required": False, "type": "path"},
            # 5. after download, move files/dirs to destinations
            "move_rules": {"required": True, "type": "list", "elements": "dict"},
        },
        supports_check_mode=False,
        mutually_exclusive=(
            ("version_file", "version_command"),
            ("version_file", "version_regex"),
        ),
        required_by={"version_regex": ["version_command"]},
    )

    repo: str = module.params["repo"]
    tag: str = module.params["tag"]
    asset_regex: re.Pattern = re.compile(module.params["asset_regex"])
    asset_arch_mapping: dict = module.params["asset_arch_mapping"]
    version_command: str = module.params["version_command"]
    version_regex = module.params["version_regex"] or r"\d+\.\d+(?:\.\d+)?"
    version_file = module.params["version_file"]
    move_rules: List[dict] = module.params["move_rules"]

    if not re.match(r"[\w\-_]+/[\w\-_]+", repo):
        module.fail_json(msg="Invalid repo")

    move_rule_schema = {
        "src_regex": {"allowed_types": [str], "required": True},
        "dst": {"allowed_types": [str], "required": True},
        "mode": {"allowed_types": [str, int], "required": False},
        "owner": {"allowed_types": [str], "required": False},
        "group": {"allowed_types": [str], "required": False},
    }
    for move_rule in move_rules:
        for k, schema in move_rule_schema.items():
            if schema.get("required") and k not in move_rule:
                module.fail_json(
                    msg=f"Some move rule does not have required argument '{k}'."
                )
            if k in move_rules and not any(
                isinstance(move_rule[k], allowed_type)
                for allowed_type in schema.get("allowed_types", [object])
            ):
                module.fail_json(
                    msg=f"Some move rule has invalid type for argument '{k}'."
                )
        move_rule["dst"] = os.path.expanduser(move_rule["dst"])

    if tag == "latest":
        # https://docs.github.com/en/rest/releases/releases#get-the-latest-release
        release_info_url = f"/repos/{repo}/releases/latest"
    else:
        # https://docs.github.com/en/rest/releases/releases#get-a-release-by-tag-name
        release_info_url = f"/repos/{repo}/releases/tags/{tag}"

    release_info = get_json_url(f"https://api.github.com{release_info_url}")

    if not is_download_required(
        module, version_command, version_regex, version_file, release_info
    ):
        module.exit_json(changed=False)

    try:
        asset = AssetSelector(asset_regex, asset_arch_mapping).select_asset(release_info["assets"])
    except AssetSelector.AssetSelectionFailed as e:
        module.fail_json(msg=str(e))
        return

    changed = download_asset(
        module, asset["name"], asset["browser_download_url"], move_rules
    )

    if version_file:
        with open(version_file, "w") as fp:
            fp.write(release_info["tag_name"])

    module.exit_json(changed=changed)


if __name__ == "__main__":
    main()

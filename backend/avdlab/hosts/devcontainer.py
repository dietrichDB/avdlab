"""Devcontainer bundle generator (pure functions; zipping runs in a thread from the API).

`generate_files(mode, ...)` returns `{relative_path: content}`; `build_zip(files)` packs them.
Modes:
  local-clab   the devcontainer *is* the lab host: docker-in-docker feature, containerlab runs inside.
  remote-host  the devcontainer only edits/builds; labs run on a remote host reached over SSH.
"""
from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Literal

from ..core.config import settings

Mode = Literal["local-clab", "remote-host"]
MODES: tuple[str, ...] = ("local-clab", "remote-host")

VSCODE_EXTENSIONS = [
    "redhat.ansible",
    "redhat.vscode-yaml",
    "ms-python.python",
    "ms-azuretools.vscode-docker",
    "eamodio.gitlens",
]

AVD_SCHEMA_URL = "https://raw.githubusercontent.com/aristanetworks/avd/v{avd}/python-avd/pyavd/_eos_designs/schema/eos_designs.jsonschema.json"


def devcontainer_json(mode: Mode, *, avd_version: str, include_vscode_extensions: bool) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "name": f"avdlab ({mode})",
        "build": {"dockerfile": "Dockerfile", "context": ".", "args": {"AVD_VERSION": avd_version}},
        "remoteUser": "avd",
        "workspaceFolder": "/home/avd/lab",
        "workspaceMount": "source=${localWorkspaceFolder},target=/home/avd/lab,type=bind,consistency=cached",
        "containerEnv": {"AVD_VERSION": avd_version, "ANSIBLE_CONFIG": "/home/avd/lab/ansible.cfg"},
        "features": {},
        "postCreateCommand": "make setup",
        "customizations": {},
    }
    if mode == "local-clab":
        doc["features"]["ghcr.io/devcontainers/features/docker-in-docker:2"] = {"moby": True, "dockerDashComposeVersion": "v2"}
        doc["privileged"] = True
        doc["runArgs"] = ["--pid=host", "--network=host", "--cap-add=NET_ADMIN", "--cap-add=SYS_ADMIN"]
        doc["mounts"] = ["source=/var/run/netns,target=/var/run/netns,type=bind"]
    else:
        doc["features"]["ghcr.io/devcontainers/features/sshd:1"] = {}
        doc["mounts"] = ["source=${localEnv:HOME}/.ssh,target=/home/avd/.ssh,type=bind,readonly"]
    if include_vscode_extensions:
        doc["customizations"]["vscode"] = {
            "extensions": VSCODE_EXTENSIONS,
            "settings": {
                "yaml.schemas": {AVD_SCHEMA_URL.format(avd=avd_version): ["group_vars/**/*.yml", "group_vars/*.yml", "host_vars/**/*.yml"]},
                "ansible.python.interpreterPath": "/usr/local/bin/python3",
                "files.associations": {"**/group_vars/**/*.yml": "ansible", "**/playbooks/*.yml": "ansible"},
            },
        }
    return doc


def dockerfile(mode: Mode, *, agent_image: str) -> str:
    lines = [
        f"FROM {agent_image}",
        "ARG AVD_VERSION",
        "USER root",
        "RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \\",
        "      curl ca-certificates gnupg jq rsync openssh-client make iproute2 \\",
        "    && rm -rf /var/lib/apt/lists/*",
        "# docker CLI (talks to the docker-in-docker daemon or the host socket)",
        "RUN install -m 0755 -d /etc/apt/keyrings \\",
        "    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \\",
        "    && . /etc/os-release \\",
        '    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list \\',
        "    && apt-get update -qq && apt-get install -y -qq --no-install-recommends docker-ce-cli docker-buildx-plugin docker-compose-plugin \\",
        "    && rm -rf /var/lib/apt/lists/*",
        "# containerlab CLI",
        'RUN curl -sL https://containerlab.dev/setup | bash -s "install-containerlab"',
        "# eos-downloader (ardl) for pulling cEOS images with an arista.com token",
        'RUN pip install --no-cache-dir "eos-downloader>=0.13" \\',
        '    && python3 -c "import pyavd, anta" || pip install --no-cache-dir "anta[cli]==' + settings.anta_version + '"',
        "RUN id avd >/dev/null 2>&1 || useradd -m -s /bin/bash avd; usermod -aG docker avd 2>/dev/null || true",
        "USER avd",
        "WORKDIR /home/avd/lab",
    ]
    if mode == "remote-host":
        lines.insert(len(lines) - 2, 'RUN mkdir -p /home/avd/.ssh && chmod 700 /home/avd/.ssh && chown -R avd:avd /home/avd/.ssh')
    return "\n".join(lines) + "\n"


def ansible_cfg() -> str:
    return (
        "[defaults]\n"
        "inventory = inventory.yml\n"
        "host_key_checking = False\n"
        "gathering = explicit\n"
        "jinja2_native = True\n"
        "duplicate_dict_key = error\n"
        "stdout_callback = yaml\n"
        "collections_path = ~/.ansible/collections:/usr/share/ansible/collections\n"
        "forks = 15\n\n"
        "[persistent_connection]\n"
        "connect_timeout = 120\n"
        "command_timeout = 120\n"
    )


def requirements_txt(avd_version: str) -> str:
    return (
        f"pyavd[ansible]=={avd_version}\n"
        f"anta[cli]=={settings.anta_version}\n"
        "eos-downloader>=0.13\n"
        "netaddr>=1.0\n"
    )


def makefile(mode: Mode, *, avd_version: str) -> str:
    clab = "sudo containerlab" if mode == "local-clab" else "containerlab"
    remote_note = "" if mode == "local-clab" else (
        "# Remote mode: set LAB_HOST=user@host (and LAB_DIR) to run containerlab over SSH.\n"
        "LAB_HOST ?=\n"
        "LAB_DIR  ?= /opt/avdlab/$(notdir $(CURDIR))\n"
    )
    if mode == "local-clab":
        clab_up = f"\t{clab} deploy -t $(TOPOLOGY) --reconfigure"
        clab_down = f"\t{clab} destroy -t $(TOPOLOGY) --cleanup"
    else:
        clab_up = (
            "\t@test -n \"$(LAB_HOST)\" || (echo 'set LAB_HOST=user@host'; exit 1)\n"
            "\tssh $(LAB_HOST) mkdir -p $(LAB_DIR)\n"
            "\trsync -az --delete --exclude .git ./ $(LAB_HOST):$(LAB_DIR)/\n"
            "\tssh $(LAB_HOST) 'cd $(LAB_DIR) && sudo containerlab deploy -t $(TOPOLOGY) --reconfigure'"
        )
        clab_down = "\tssh $(LAB_HOST) 'cd $(LAB_DIR) && sudo containerlab destroy -t $(TOPOLOGY) --cleanup'"
    return (
        f"# avdlab devcontainer Makefile ({mode}, AVD {avd_version})\n"
        "AVD_VERSION ?= " + avd_version + "\n"
        "TOPOLOGY    ?= clab/topology.clab.yml\n"
        f"{remote_note}"
        "\n.PHONY: setup build deploy validate clab-up clab-down clean\n\n"
        "setup:  ## install/verify the AVD collection and python deps\n"
        "\tpip install --user -q -r requirements.txt\n"
        "\tansible-galaxy collection install --upgrade arista.avd:==$(AVD_VERSION)\n\n"
        "build:  ## render intended configs + docs (eos_designs + eos_cli_config_gen)\n"
        "\tansible-playbook playbooks/build.yml\n\n"
        "deploy: ## push configs to the running devices with eos_config_deploy_eapi\n"
        "\tansible-playbook playbooks/deploy.yml\n\n"
        "validate: ## run ANTA against the fabric (eos_validate_state)\n"
        "\tansible-playbook playbooks/validate.yml\n\n"
        "clab-up: ## deploy the containerlab topology\n"
        f"{clab_up}\n\n"
        "clab-down: ## destroy the containerlab topology\n"
        f"{clab_down}\n\n"
        "clean:\n"
        "\trm -rf intended documentation reports\n"
    )


def readme(mode: Mode, *, avd_version: str) -> str:
    where = ("This devcontainer runs Docker-in-Docker, so `make clab-up` deploys containerlab **inside** the devcontainer.\n"
             "Give Docker Desktop / the VM enough RAM (about 2 GB per cEOS node)."
             if mode == "local-clab" else
             "Labs run on a remote host prepared by avdlab (Hosts -> Bootstrap). Your `~/.ssh` is mounted read-only; set\n"
             "`LAB_HOST=user@host` (and optionally `LAB_DIR`) so `make clab-up` can rsync the lab and run containerlab there.")
    return f"""# avdlab devcontainer ({mode})

Generated by avdlab for AVD {avd_version}.

1. Unzip this bundle into your lab directory (next to `inventory.yml`, `group_vars/`, `playbooks/`).
2. Open the folder in VS Code and choose **Reopen in Container** (needs the Dev Containers extension).
3. In the container terminal: `make build`, then `make clab-up`, `make deploy`, `make validate`.

{where}

## Layout

| file | purpose |
|---|---|
| `.devcontainer/devcontainer.json` | container definition ({mode}) |
| `.devcontainer/Dockerfile` | `{settings.agent_image}` + docker CLI, containerlab, eos-downloader |
| `ansible.cfg` | AVD-recommended ansible settings |
| `requirements.txt` | pinned `pyavd[ansible]`, `anta` |
| `Makefile` | `build`, `deploy`, `validate`, `clab-up`, `clab-down` |

## cEOS images

`ardl get eos --format cEOS --version <version> --import-docker` (needs `ARISTA_TOKEN` from arista.com), or push an image
from the avdlab Images page{' to the lab host' if mode == 'remote-host' else ''}.
"""


def generate_files(mode: str, *, avd_version: str | None = None, include_vscode_extensions: bool = True,
                   agent_image: str | None = None) -> dict[str, str]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    m: Mode = mode  # type: ignore[assignment]
    avd = avd_version or settings.avd_version
    image = agent_image or settings.agent_image
    return {
        ".devcontainer/devcontainer.json": json.dumps(devcontainer_json(m, avd_version=avd, include_vscode_extensions=include_vscode_extensions), indent=2) + "\n",
        ".devcontainer/Dockerfile": dockerfile(m, agent_image=image),
        "ansible.cfg": ansible_cfg(),
        "requirements.txt": requirements_txt(avd),
        "Makefile": makefile(m, avd_version=avd),
        "README.md": readme(m, avd_version=avd),
    }


def build_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()

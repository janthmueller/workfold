"""Isolated local Git repositories with exact timestamp fixtures."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(slots=True)
class GitRepo:
    """Small plumbing-based repository fixture independent of user config."""

    path: Path
    branch: str = "main"

    @classmethod
    def create(cls, path: Path) -> GitRepo:
        path.mkdir(parents=True)
        repo = cls(path=path)
        repo.run("init", "--initial-branch=main")
        return repo

    def _environment(self, additions: Mapping[str, str] | None = None) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "LANG": "C",
                "LC_ALL": "C",
            }
        )
        if additions is not None:
            environment.update(additions)
        return environment

    def run(
        self,
        *arguments: str,
        input_data: bytes | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> bytes:
        completed = subprocess.run(
            (
                "git",
                "--no-pager",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *arguments,
            ),
            cwd=self.path,
            env=self._environment(environment),
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace")
            raise AssertionError(f"fixture Git command failed ({completed.returncode}): {stderr}")
        return completed.stdout

    def commit(
        self,
        filename: str,
        content: str,
        subject: str,
        *,
        author_date: str,
        committer_date: str,
        author_name: str = "Fixture Author",
        author_email: str = "author@example.test",
        committer_name: str = "Fixture Committer",
        committer_email: str = "committer@example.test",
        parent: str | None = None,
        update_ref: str | None = None,
    ) -> str:
        target = self.path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.run("add", "--", filename)
        tree = self.run("write-tree").decode("ascii").strip()
        if parent is None:
            parent_result = subprocess.run(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                cwd=self.path,
                env=self._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
            )
            parent = parent_result.stdout.decode("ascii").strip() if parent_result.returncode == 0 else None

        arguments = ["commit-tree", tree]
        if parent is not None:
            arguments.extend(("-p", parent))
        identity_environment = {
            "GIT_AUTHOR_DATE": author_date,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_AUTHOR_NAME": author_name,
            "GIT_COMMITTER_DATE": committer_date,
            "GIT_COMMITTER_EMAIL": committer_email,
            "GIT_COMMITTER_NAME": committer_name,
        }
        commit_id = (
            self.run(
                *arguments,
                input_data=subject.encode("utf-8") + b"\n",
                environment=identity_environment,
            )
            .decode("ascii")
            .strip()
        )
        ref_name = update_ref or f"refs/heads/{self.branch}"
        self.run("update-ref", ref_name, commit_id)
        return commit_id

    def point_ref(self, ref_name: str, object_id: str) -> None:
        self.run("update-ref", ref_name, object_id)

    def detach(self, object_id: str) -> None:
        self.run("update-ref", "--no-deref", "HEAD", object_id)

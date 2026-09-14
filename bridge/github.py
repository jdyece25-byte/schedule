"""Small GitHub client. Credentials stay in memory, obtained from gh's keyring."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request


class GitHubError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f"GitHub HTTP {status}: {message}")


class GitHub:
    def __init__(self, token=None):
        if token is None:
            result = subprocess.run(
                ["gh", "auth", "token", "--hostname", "github.com"],
                capture_output=True, encoding="utf-8", timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode or not result.stdout.strip():
                raise RuntimeError("GitHub 로그인이 필요합니다. PC에서 gh auth login을 실행하세요.")
            token = result.stdout.strip()
        self._token = token

    def api(self, endpoint, method="GET", data=None):
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            "https://api.github.com/" + endpoint.lstrip("/"), data=body, method=method,
            headers={"Authorization": "Bearer " + self._token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json", "User-Agent": "ScheduleBridge/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            # Never log headers, request content, or credentials.
            try:
                message = json.loads(error.read()).get("message", "요청 실패")
            except (ValueError, UnicodeError):
                message = "요청 실패"
            raise GitHubError(error.code, message) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise GitHubError(0, "네트워크 연결을 확인할 수 없습니다.") from None

    def read(self, repo, path, ref="main"):
        endpoint = f"repos/{repo}/contents/{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(ref, safe='')}"
        try:
            value = self.api(endpoint)
        except GitHubError as error:
            if error.status == 404:
                return None, None
            raise
        if value.get("type") != "file" or value.get("encoding") != "base64":
            raise RuntimeError("예상하지 않은 GitHub 파일 형식입니다.")
        return base64.b64decode(value["content"]).decode("utf-8"), value["sha"]

    def read_json(self, repo, path, ref="main"):
        text, sha = self.read(repo, path, ref)
        return (None if text is None else json.loads(text)), sha

    def put_json(self, repo, path, value, sha=None, branch="main", message="Update schedule request state"):
        content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        payload = {"message": message, "content": base64.b64encode(content.encode()).decode(), "branch": branch}
        if sha:
            payload["sha"] = sha
        result = self.api(f"repos/{repo}/contents/{urllib.parse.quote(path, safe='/')}", "PUT", payload)
        return result["content"]["sha"]

    def head(self, repo, branch="main"):
        return self.api(f"repos/{repo}/git/ref/heads/{urllib.parse.quote(branch, safe='')}")["object"]["sha"]

    def tree(self, repo, ref="main"):
        value = self.api(f"repos/{repo}/git/trees/{urllib.parse.quote(ref, safe='')}?recursive=1")
        if value.get("truncated"):
            raise RuntimeError("요청 저장소가 너무 큽니다. 처리 완료 요청을 정리해 주세요.")
        return {item["path"]: item["sha"] for item in value["tree"] if item["type"] == "blob"}

    def commit_files(self, repo, branch, base, files, message):
        """One atomic fast-forward: a changed branch rejects the entire update."""
        parent = self.api(f"repos/{repo}/git/commits/{base}")
        entries = [{"path": path, "mode": "100644", "type": "blob", "content": content}
                   for path, content in files.items()]
        tree = self.api(f"repos/{repo}/git/trees", "POST", {"base_tree": parent["tree"]["sha"], "tree": entries})
        commit = self.api(f"repos/{repo}/git/commits", "POST", {
            "message": message, "tree": tree["sha"], "parents": [base],
        })
        self.api(f"repos/{repo}/git/refs/heads/{urllib.parse.quote(branch, safe='')}", "PATCH", {
            "sha": commit["sha"], "force": False,
        })
        return commit["sha"]

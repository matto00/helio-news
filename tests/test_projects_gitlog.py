import subprocess

from news.projects import gitlog


def _init_repo(tmp_path, subjects: list[str]):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    for i, subject in enumerate(subjects):
        (repo / f"file{i}.txt").write_text(subject)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", subject], cwd=repo, check=True)
    return repo


def test_fetch_recent_subjects_returns_most_recent_first(tmp_path):
    repo = _init_repo(tmp_path, ["HEL-1 First commit", "HEL-2 Second commit", "HEL-3 Third commit"])

    subjects = gitlog.fetch_recent_subjects(str(repo), since_days=30)

    assert subjects == ["HEL-3 Third commit", "HEL-2 Second commit", "HEL-1 First commit"]


def test_fetch_recent_subjects_returns_empty_list_for_nonexistent_path(tmp_path):
    missing = tmp_path / "does-not-exist"

    assert gitlog.fetch_recent_subjects(str(missing), since_days=30) == []


def test_fetch_recent_subjects_returns_empty_list_for_non_git_directory(tmp_path):
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()

    assert gitlog.fetch_recent_subjects(str(not_a_repo), since_days=30) == []

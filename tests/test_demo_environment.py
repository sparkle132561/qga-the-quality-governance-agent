import pytest
from quality.demo_environment import verify_marker


class MarkerConnection:
    def __init__(self, database, workspace, mode="fake_only", marker=True):
        self.database, self.workspace, self.mode, self.marker = database, workspace, mode, marker

    def execute(self, query):
        if "current_database" in query:
            self.value = (self.database,)
        elif "to_regclass" in query:
            self.value = ("codex_demo_environment" if self.marker else None,)
        else:
            self.value = (self.workspace, self.mode)
        return self

    def fetchone(self):
        return self.value


@pytest.mark.parametrize(
    "database,owner,mode,marker",
    [
        ("quality", "own", "fake_only", True),
        ("quality_test", "own", "fake_only", True),
        ("quality_demo", "other", "fake_only", True),
        ("quality_demo", "own", "live", True),
        ("quality_demo", "own", "fake_only", False),
    ],
)
def test_demo_marker_refuses_other_database_workspace_mode_or_unmarked_data(
    database, owner, mode, marker, tmp_path
):
    root = tmp_path / "own"
    connection = MarkerConnection(
        database, str(root if owner == "own" else tmp_path / "other"), mode, marker
    )
    with pytest.raises(RuntimeError):
        verify_marker(connection, root)


def test_demo_marker_accepts_only_owned_fake_environment(tmp_path):
    root = tmp_path / "own"
    verify_marker(MarkerConnection("quality_demo", str(root)), root)

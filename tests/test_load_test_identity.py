from pathlib import Path
from runpy import run_path

from codechef_task.user_identity import require_user_ref

LOAD_TEST_USER_REF = run_path(
    str(Path(__file__).parents[1] / "scripts" / "load_test.py")
)["LOAD_TEST_USER_REF"]


def test_load_test_identity_matches_registration_number_validator() -> None:
    assert require_user_ref(LOAD_TEST_USER_REF) == "LOADTEST01"

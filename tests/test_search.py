"""Tests for level 1 of the retrieval protocol: finding candidate files."""

from aetron.analyzer import analyze
from aetron.context.search import search, words
from aetron.scanner import scan


def find(make_project, layout, query, **kwargs):
    root = make_project(layout)
    scan_result = scan(root)
    return search(scan_result, analyze(scan_result), query, **kwargs)


def paths(candidates):
    return [c.rel_path for c in candidates]


class TestWordSplitting:
    def test_snake_case(self):
        assert words("login_handler") == ["login", "handler"]

    def test_camel_and_pascal_case(self):
        assert words("LoginController") == ["login", "controller"]
        assert words("parseLoginForm") == ["parse", "login", "form"]

    def test_runs_of_capitals_stay_together(self):
        assert words("HTTPServer") == ["http", "server"]

    def test_digits_are_their_own_word(self):
        assert words("parse2JSON") == ["parse", "2", "json"]


class TestFindingDefinitions:
    def test_exact_symbol_name_wins(self, make_project):
        layout = {
            "auth.py": "def login():\n    pass\n",
            "other.py": "def login_helper():\n    pass\n",
        }
        assert paths(find(make_project, layout, "login"))[0] == "auth.py"

    def test_a_word_inside_an_identifier_matches(self, make_project):
        layout = {"a.py": "class LoginController:\n    pass\n"}
        assert paths(find(make_project, layout, "login")) == ["a.py"]

    def test_the_reason_names_the_definition(self, make_project):
        layout = {"a.py": "class LoginController:\n    def handle(self):\n        pass\n"}
        assert "class LoginController" in find(make_project, layout, "login")[0].reason

    def test_the_line_of_the_definition_is_reported(self, make_project):
        layout = {"a.py": "x = 1\n\n\ndef login():\n    pass\n"}
        assert find(make_project, layout, "login")[0].best_line == 4

    def test_unrelated_files_are_not_candidates(self, make_project):
        layout = {"auth.py": "def login():\n    pass\n", "math.py": "def add(a, b):\n    return a + b\n"}
        assert paths(find(make_project, layout, "login")) == ["auth.py"]


class TestEvidenceAccumulates:
    def test_more_evidence_scores_higher(self, make_project):
        layout = {
            "login.py": "class LoginController:\n    def login_handler(self):\n        pass\n",
            "other.py": "class LoginController:\n    pass\n",
        }
        results = find(make_project, layout, "login")
        assert results[0].rel_path == "login.py"
        assert results[0].score > results[1].score

    def test_a_score_never_reaches_certainty(self, make_project):
        body = "\n".join(f"def login_{n}():\n    pass\n" for n in range(40))
        layout = {"login.py": f"class Login:\n    pass\n{body}"}
        assert find(make_project, layout, "login")[0].score < 1.0

    def test_weak_evidence_stays_weak(self, make_project):
        layout = {
            "defines.py": "def login():\n    pass\n",
            "calls.py": "from defines import login\n\n\ndef go():\n    return login()\n",
        }
        results = find(make_project, layout, "login")
        assert results[0].rel_path == "defines.py"


class TestUnparsedLanguages:
    """The protocol's worked example is a .cs file and there is no C# parser.

    Search has to work on names alone, or the one case the design was written
    around returns nothing.
    """

    def test_a_file_with_no_parser_is_still_found(self, make_project):
        layout = {"logincontroller.go": "package main\n"}
        assert paths(find(make_project, layout, "login")) == ["logincontroller.go"]

    def test_the_candidate_says_it_could_not_be_parsed(self, make_project):
        layout = {"logincontroller.go": "package main\n"}
        assert find(make_project, layout, "login")[0].parsed is False

    def test_a_parsed_candidate_says_so(self, make_project):
        layout = {"login.py": "def login():\n    pass\n"}
        assert find(make_project, layout, "login")[0].parsed is True

    def test_a_directory_name_is_evidence(self, make_project):
        layout = {"login/handlers.py": "def run():\n    pass\n"}
        assert paths(find(make_project, layout, "login")) == ["login/handlers.py"]


class TestMultipleTerms:
    def test_matching_every_term_beats_matching_one(self, make_project):
        layout = {
            "both.py": "def user_login():\n    pass\n",
            "one.py": "def login():\n    pass\n",
        }
        results = find(make_project, layout, "user login")
        assert results[0].rel_path == "both.py"

    def test_a_query_written_as_one_identifier_is_the_same_question(self, make_project):
        layout = {"a.py": "def user_login():\n    pass\n"}
        spaced = find(make_project, layout, "user login")
        joined = find(make_project, layout, "userLogin")
        assert spaced[0].score == joined[0].score


class TestOutputContract:
    def test_nothing_matching_returns_nothing(self, make_project):
        layout = {"a.py": "def add(a, b):\n    return a + b\n"}
        assert find(make_project, layout, "login") == []

    def test_an_empty_query_returns_nothing(self, make_project):
        assert find(make_project, {"a.py": "def login():\n    pass\n"}, "   ") == []

    def test_punctuation_only_query_returns_nothing(self, make_project):
        assert find(make_project, {"a.py": "def login():\n    pass\n"}, "??") == []

    def test_the_limit_is_honoured(self, make_project):
        layout = {f"login_{n}.py": "def go():\n    pass\n" for n in range(10)}
        assert len(find(make_project, layout, "login", limit=3)) == 3

    def test_percent_is_a_whole_number_in_range(self, make_project):
        layout = {"a.py": "def login():\n    pass\n"}
        percent = find(make_project, layout, "login")[0].percent
        assert isinstance(percent, int) and 0 < percent <= 100

    def test_results_are_ordered_by_score(self, make_project):
        layout = {
            "login.py": "class Login:\n    def login(self):\n        pass\n",
            "helpers.py": "def maybe_login_later():\n    pass\n",
            "notes.py": "def unrelated():\n    pass\n",
        }
        scores = [c.score for c in find(make_project, layout, "login")]
        assert scores == sorted(scores, reverse=True)

    def test_the_same_search_twice_gives_the_same_order(self, make_project):
        layout = {f"mod_{n}/login.py": "def go():\n    pass\n" for n in range(6)}
        root = make_project(layout)
        scan_result = scan(root)
        analysis = analyze(scan_result)
        first = search(scan_result, analysis, "login")
        second = search(scan_result, analysis, "login")
        assert paths(first) == paths(second)

    def test_no_source_code_is_returned(self, make_project):
        layout = {"a.py": "def login():\n    secret = 'do not leak this'\n"}
        assert "do not leak this" not in str(find(make_project, layout, "login"))


class TestWholeQueryNames:
    """A name written the way code writes the question is the best answer
    search can give, however the identifier happens to be spelled."""

    def test_a_name_matching_the_whole_query_wins(self, make_project):
        layout = {
            "LoginHandler.py": "class LoginHandler:\n    pass\n",
            "other.py": "def login():\n    pass\n",
        }
        assert paths(find(make_project, layout, "login handler"))[0] == "LoginHandler.py"

    def test_spelling_does_not_matter(self, make_project):
        spellings = ["LoginHandler", "login_handler", "loginHandler", "loginhandler"]
        for spelling in spellings:
            layout = {"a.py": f"def {spelling}():\n    pass\n"}
            top = find(make_project, layout, "login handler")[0]
            assert top.percent > 80, f"{spelling} scored {top.percent}"

    def test_a_run_together_file_name_is_found(self, make_project):
        """Found against the standard library: "socket server" returned
        everything except socketserver.py."""
        layout = {
            "socketserver.py": "class BaseServer:\n    pass\n",
            "noise.py": "def unrelated():\n    pass\n",
        }
        assert paths(find(make_project, layout, "socket server"))[0] == "socketserver.py"

    def test_one_fact_is_not_counted_twice(self, make_project):
        layout = {"LoginHandler.py": "x = 1\n"}
        reason = find(make_project, layout, "login handler")[0].reason
        assert reason.count("LoginHandler") == 1


class TestRanking:
    """Evidence about a file is correlated, not independent. Treating it as
    independent walked every plausible file to 99% and flattened the top of
    the ranking, which is the part that has to discriminate."""

    def test_scores_are_spread_not_saturated(self, make_project):
        layout = {
            "login.py": "class Login:\n    def login(self):\n        pass\n",
            "views.py": "class LoginView:\n    pass\n",
            "misc.py": "def maybe_relogin():\n    pass\n",
        }
        scores = [c.percent for c in find(make_project, layout, "login")]
        assert len(scores) == 3
        assert max(scores) - min(scores) > 20

    def test_a_big_file_does_not_win_on_volume(self, make_project):
        """The long-document problem: a large file matches more of everything
        without being a better answer."""
        big = "\n".join(f"def login_step_{n}():\n    pass\n" for n in range(200))
        layout = {"huge.py": big, "login.py": "def login():\n    pass\n"}
        assert paths(find(make_project, layout, "login"))[0] == "login.py"

    def test_a_score_is_never_reported_as_certain(self, make_project):
        layout = {"login.py": "class Login:\n    def login(self):\n        pass\n"}
        assert find(make_project, layout, "login")[0].percent <= 99

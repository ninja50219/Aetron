"""Tests for dependency extraction from manifest files."""

from aetron.scanner.manifests import is_manifest, parse_manifest


def names(deps):
    return {d.name for d in deps}


def versions(deps):
    return {d.name: d.version for d in deps}


class TestRecognition:
    def test_known_names(self):
        assert is_manifest("package.json")
        assert is_manifest("PYPROJECT.TOML")
        assert is_manifest("go.mod")

    def test_variable_names_by_suffix(self):
        assert is_manifest("MyGame.csproj")

    def test_source_file_is_not_a_manifest(self):
        assert not is_manifest("main.py")


class TestNode:
    def test_collects_all_sections(self):
        text = """
        {"dependencies": {"react": "^18.2.0"},
         "devDependencies": {"vite": "5.0.0"},
         "peerDependencies": {"typescript": "*"}}
        """
        deps = parse_manifest("package.json", text, "package.json")
        assert names(deps) == {"react", "vite", "typescript"}
        assert versions(deps)["react"] == "^18.2.0"
        assert {d.ecosystem for d in deps} == {"node"}


class TestPython:
    def test_requirements_txt(self):
        text = "requests>=2.31\n# comment\n\nflask==3.0.0\n-r other.txt\n"
        deps = parse_manifest("requirements.txt", text, "requirements.txt")
        assert versions(deps) == {"requests": ">=2.31", "flask": "==3.0.0"}

    def test_requirements_with_extras(self):
        deps = parse_manifest("requirements.txt", "uvicorn[standard]>=0.27\n", "r.txt")
        assert versions(deps) == {"uvicorn": ">=0.27"}

    def test_pep621_dependencies(self):
        text = '[project]\nname = "x"\ndependencies = ["requests>=2.31", "pathspec"]\n'
        deps = parse_manifest("pyproject.toml", text, "pyproject.toml")
        assert versions(deps) == {"requests": ">=2.31", "pathspec": "*"}

    def test_poetry_table_and_python_entry(self):
        text = (
            "[tool.poetry.dependencies]\n"
            'python = "^3.11"\n'
            'requests = "^2.31"\n'
            'pandas = {version = "2.1", optional = true}\n'
        )
        deps = parse_manifest("pyproject.toml", text, "pyproject.toml")
        # The python interpreter is a constraint, not a dependency.
        assert versions(deps) == {"requests": "^2.31", "pandas": "2.1"}


class TestRust:
    def test_plain_and_table_versions(self):
        text = (
            "[dependencies]\n"
            'tokio = "1.35"\n'
            'serde = {version = "1.0", features = ["derive"]}\n'
            "[dev-dependencies]\n"
            'criterion = "0.5"\n'
        )
        deps = parse_manifest("Cargo.toml", text, "Cargo.toml")
        assert versions(deps) == {"tokio": "1.35", "serde": "1.0", "criterion": "0.5"}


class TestGo:
    def test_require_block_and_single_line(self):
        text = (
            "module example.com/x\n\n"
            "go 1.21\n\n"
            "require (\n"
            "\tgithub.com/gin-gonic/gin v1.9.1\n"
            "\tgolang.org/x/sync v0.5.0 // indirect\n"
            ")\n\n"
            "require github.com/spf13/cobra v1.8.0\n"
        )
        deps = parse_manifest("go.mod", text, "go.mod")
        assert names(deps) == {
            "github.com/gin-gonic/gin",
            "golang.org/x/sync",
            "github.com/spf13/cobra",
        }


class TestDotnetAndJava:
    def test_package_reference(self):
        text = (
            '<Project><ItemGroup>'
            '<PackageReference Include="Newtonsoft.Json" Version="13.0.3" />'
            "</ItemGroup></Project>"
        )
        deps = parse_manifest("App.csproj", text, "App.csproj")
        assert versions(deps) == {"Newtonsoft.Json": "13.0.3"}

    def test_maven_dependency_keeps_group_id(self):
        text = (
            '<project xmlns="http://maven.apache.org/POM/4.0.0"><dependencies>'
            "<dependency><groupId>org.junit</groupId>"
            "<artifactId>junit</artifactId><version>5.10.0</version></dependency>"
            "</dependencies></project>"
        )
        deps = parse_manifest("pom.xml", text, "pom.xml")
        assert versions(deps) == {"org.junit:junit": "5.10.0"}


class TestRobustness:
    def test_malformed_json_yields_nothing(self):
        # One broken manifest must never end a scan of many projects.
        assert parse_manifest("package.json", "{not json", "package.json") == []

    def test_malformed_xml_yields_nothing(self):
        assert parse_manifest("App.csproj", "<Project><oops", "App.csproj") == []

    def test_malformed_toml_yields_nothing(self):
        assert parse_manifest("Cargo.toml", "[[[", "Cargo.toml") == []

    def test_unknown_manifest_yields_nothing(self):
        assert parse_manifest("notes.txt", "hello", "notes.txt") == []

    def test_manifest_path_is_recorded(self):
        deps = parse_manifest("package.json", '{"dependencies":{"a":"1"}}', "web/package.json")
        assert deps[0].manifest == "web/package.json"

"""Tests for reading JavaScript and TypeScript without a JS engine.

This family has more ways to write a function than any other language Aetron
reads, and someone searching for where something happens does not care which
one was used. Most of these check that a form is found; the rest check that
something which merely looks like a declaration is not claimed as one.
"""

from aetron.analyzer.javascript_parser import parse
from aetron.analyzer.symbols import SymbolKind


def definitions(result):
    return [s for s in result.symbols if s.kind != SymbolKind.MODULE]


def names(result):
    return [s.name for s in definitions(result)]


def named(result, name):
    return next(s for s in result.symbols if s.name == name)


APP = """import React, { useState } from "react";
import type { User } from "./types";
const db = require("./database");

export interface LoginProps extends BaseProps {
  onSuccess: (user: User) => void;
}

export enum Status { Ok, Failed }

export async function loginHandler(username, password) {
  const hashed = await hash(password);
  function nestedHelper() { return 1; }
  return db.find({ username, hashed });
}

export const validateLogin = (user) => {
  const pattern = /^[a-z]+\\/[0-9]+$/;
  return pattern.test(user.name);
};

const shorthand = x => x * 2;

export default class LoginController extends Controller {
  #secret = "hidden";
  static instances = 0;

  constructor(store) {
    this.store = store;
  }

  async handleLogin({ username, password }, ...rest) {
    const message = `signing in ${username} { not a brace }`;
    return this.store.check(username, password);
  }

  get isReady() { return true; }
}
"""


class TestTheManyWaysToWriteAFunction:
    def test_a_declaration(self):
        assert "loginHandler" in names(parse(APP, "a.js"))

    def test_an_arrow_assigned_to_a_name(self):
        assert "validateLogin" in names(parse(APP, "a.js"))

    def test_an_arrow_with_one_bare_parameter(self):
        assert named(parse(APP, "a.js"), "shorthand").parameters == ["x"]

    def test_a_function_expression_assigned_to_a_name(self):
        source = "const go = function (a, b) {\n  return a;\n};\n"
        assert named(parse(source, "a.js"), "go").parameters == ["a", "b"]

    def test_a_class_method(self):
        assert "handleLogin" in names(parse(APP, "a.js"))

    def test_a_constructor(self):
        """A method people search for, not a keyword to skip past."""
        assert named(parse(APP, "a.js"), "constructor").parameters == ["store"]

    def test_a_getter(self):
        assert "isReady" in names(parse(APP, "a.js"))

    def test_parameters_are_named_even_when_typed(self):
        source = "export function go(user: User, count: number = 3) {\n}\n"
        assert named(parse(source, "a.ts"), "go").parameters == ["user", "count"]

    def test_a_destructured_parameter_reports_what_it_binds(self):
        assert named(parse(APP, "a.js"), "handleLogin").parameters == [
            "username",
            "password",
            "rest",
        ]


class TestTypescript:
    def test_an_interface_is_a_type(self):
        symbol = named(parse(APP, "a.ts"), "LoginProps")
        assert symbol.kind == SymbolKind.CLASS
        assert symbol.bases == ["BaseProps"]

    def test_a_class_reports_what_it_extends_and_implements(self):
        """Not cosmetic: dead code detection uses base classes to recognise a
        framework subclass, whose methods the framework calls rather than the
        project."""
        source = "export default class X extends Component implements A, B {\n}\n"
        assert named(parse(source, "a.ts"), "X").bases == ["Component", "A", "B"]

    def test_a_generic_base_keeps_its_arguments(self):
        source = "export class G<T> extends Base<T> {\n}\n"
        assert named(parse(source, "a.ts"), "G").bases == ["Base<T>"]

    def test_an_interface_member(self):
        assert "onSuccess" in names(parse(APP, "a.ts"))

    def test_an_enum(self):
        assert "Status" in names(parse(APP, "a.ts"))

    def test_a_type_alias(self):
        assert "LoginState" in names(parse('export type LoginState = "a" | "b";\n', "a.ts"))

    def test_the_language_comes_from_the_extension(self):
        assert parse("const a = 1;\n", "a.ts").language == "typescript"
        assert parse("const a = 1;\n", "a.js").language == "javascript"

    def test_tsx_is_typescript(self):
        assert parse("const a = 1;\n", "a.tsx").language == "typescript"


class TestImports:
    def test_a_named_import(self):
        found = next(i for i in parse(APP, "a.js").imports if i.module == "react")
        assert found.names == ["React", "useState"]

    def test_a_relative_import(self):
        assert any(i.module == "./types" for i in parse(APP, "a.ts").imports)

    def test_require(self):
        assert any(i.module == "./database" for i in parse(APP, "a.js").imports)

    def test_a_bare_side_effect_import(self):
        assert [i.module for i in parse('import "./styles.css";\n', "a.js").imports] == [
            "./styles.css"
        ]

    def test_export_from(self):
        assert [i.module for i in parse('export * from "./lib";\n', "a.js").imports] == [
            "./lib"
        ]

    def test_a_dynamic_import(self):
        source = 'const mod = await import("./lazy");\n'
        assert [i.module for i in parse(source, "a.js").imports] == ["./lazy"]

    def test_a_commented_out_import_is_not_an_import(self):
        source = '// import Fake from "./fake";\nimport Real from "./real";\n'
        assert [i.module for i in parse(source, "a.js").imports] == ["./real"]


class TestNothingIsInvented:
    def test_a_local_inside_a_function_is_not_top_level(self):
        assert "hashed" not in names(parse(APP, "a.js"))

    def test_a_nested_function_is_not_the_files_to_offer(self):
        assert "nestedHelper" not in names(parse(APP, "a.js"))

    def test_a_one_line_block_does_not_swallow_the_rest_of_the_file(self):
        """An enum written on one line opens no body. Treating it as a scope
        left one that could never close, and every later line in the file was
        judged against it."""
        result = parse(APP, "a.ts")
        assert "loginHandler" in names(result)
        assert "hashed" not in names(result)

    def test_a_brace_in_a_template_literal_is_not_a_block(self):
        assert named(parse(APP, "a.js"), "isReady").line > 0

    def test_a_regex_literal_containing_a_slash_does_not_swallow_the_line(self):
        assert "validateLogin" in names(parse(APP, "a.js"))

    def test_a_control_statement_is_not_a_function(self):
        source = (
            "export function go() {\n  if (x) { }\n  for (const a of b) { }\n"
            "  while (y) { }\n}\n"
        )
        assert names(parse(source, "a.js")) == ["go"]

    def test_a_method_call_is_not_a_declaration(self):
        source = "export function go() {\n  doThing(1);\n  other.call(2);\n}\n"
        assert names(parse(source, "a.js")) == ["go"]


class TestSpans:
    def test_a_method_span_covers_its_body(self):
        symbol = named(parse(APP, "a.js"), "handleLogin")
        assert symbol.end_line >= symbol.line + 3

    def test_braces_in_a_parameter_list_do_not_end_the_method(self):
        """A destructured parameter is an ordinary signature here, and
        counting its braces ended every such method on its own first line."""
        symbol = named(parse(APP, "a.js"), "handleLogin")
        assert symbol.end_line > symbol.line

    def test_a_class_span_covers_its_members(self):
        symbol = named(parse(APP, "a.js"), "LoginController")
        assert symbol.end_line >= symbol.line + 10


class TestRobustness:
    def test_an_empty_file(self):
        result = parse("", "a.js")
        assert result.parse_error is None
        assert definitions(result) == []

    def test_an_unterminated_string_does_not_swallow_the_file(self):
        source = 'const a = "unterminated\nexport function go() { }\n'
        assert "go" in names(parse(source, "a.js"))

    def test_an_unterminated_comment_does_not_raise(self):
        assert parse("/* never closed\nfunction go() {}\n", "a.js") is not None

    def test_a_file_is_always_a_module_symbol(self):
        module = next(
            s for s in parse("", "src/App.tsx").symbols if s.kind == SymbolKind.MODULE
        )
        assert module.name == "App"

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from benchmarks.loader import (
    _load_directory,
    _load_file,
    _parse_expect,
    _parse_item,
    _parse_step,
    load_benchmark,
)
from benchmarks.models import BenchExpect, BenchItem, BenchStep


class TestParseExpect:
    def test_empty(self):
        e = _parse_expect({})
        assert isinstance(e, BenchExpect)
        assert e.tools == []
        assert e.keywords_include == []

    def test_full(self):
        e = _parse_expect({
            "tools": ["exec"],
            "skills": ["coding"],
            "keywords_include": ["hello"],
            "keywords_exclude": ["error"],
            "max_iterations": 5,
            "match_type": "llm_judge",
            "check_file": "out.txt",
            "check_file_content": "done",
        })
        assert e.tools == ["exec"]
        assert e.skills == ["coding"]
        assert e.keywords_include == ["hello"]
        assert e.keywords_exclude == ["error"]
        assert e.max_iterations == 5
        assert e.match_type == "llm_judge"
        assert e.check_file == "out.txt"
        assert e.check_file_content == "done"

    def test_partial(self):
        e = _parse_expect({"tools": ["exec"]})
        assert e.tools == ["exec"]
        assert e.skills == []


class TestParseStep:
    def test_with_explicit_step(self):
        s = _parse_step({"step": 3, "question": "Q?", "weight": 2.0}, 1)
        assert s.step == 3
        assert s.question == "Q?"
        assert s.weight == 2.0

    def test_with_default_step(self):
        s = _parse_step({"question": "Q?"}, 5)
        assert s.step == 5
        assert s.question == "Q?"
        assert s.weight == 1.0

    def test_missing_question_raises(self):
        with pytest.raises(KeyError):
            _parse_step({"step": 1}, 1)

    def test_with_expect(self):
        s = _parse_step({"question": "Q?", "expect": {"tools": ["exec"]}}, 1)
        assert s.expect.tools == ["exec"]


class TestParseItem:
    def test_single_minimal(self):
        item = _parse_item({
            "id": "test-1",
            "question": "What is 2+2?",
        })
        assert item.id == "test-1"
        assert item.name == "test-1"
        assert item.difficulty == 5
        assert item.category == "general"
        assert item.type == "single"
        assert item.question == "What is 2+2?"
        assert item.steps == []
        assert item.new_session is True
        assert item.max_iterations == 30
        assert item.timeout == 60
        assert item.context_files == []

    def test_single_full(self):
        item = _parse_item({
            "id": "full-1",
            "name": "Full item",
            "difficulty": 8,
            "category": "coding",
            "type": "single",
            "new_session": False,
            "question": "Write code",
            "context_files": ["readme.txt"],
            "max_iterations": 50,
            "timeout": 120,
            "expect": {"tools": ["exec"]},
        })
        assert item.name == "Full item"
        assert item.difficulty == 8
        assert item.category == "coding"
        assert item.new_session is False
        assert item.context_files == ["readme.txt"]
        assert item.max_iterations == 50
        assert item.timeout == 120
        assert item.expect.tools == ["exec"]

    def test_multi_step(self):
        item = _parse_item({
            "id": "multi-1",
            "type": "multi_step",
            "steps": [
                {"question": "Step 1"},
                {"question": "Step 2"},
            ],
        })
        assert item.type == "multi_step"
        assert len(item.steps) == 2
        assert item.steps[0].step == 1
        assert item.steps[1].step == 2
        assert item.steps[0].question == "Step 1"

    def test_multi_step_no_steps_raises(self):
        with pytest.raises(ValueError, match="has type multi_step but no steps defined"):
            _parse_item({
                "id": "bad-multi",
                "type": "multi_step",
                "steps": [],
            })

    def test_missing_id_raises(self):
        with pytest.raises(KeyError):
            _parse_item({"question": "Q?"})


class TestLoadFile:
    def test_list_format(self, temp_yaml_file):
        suite = _load_file(temp_yaml_file)
        assert suite.name == temp_yaml_file.stem
        assert len(suite.items) == 1
        assert suite.items[0].id == "yaml-1"
        assert suite.items[0].question == "Test?"

    def test_dict_format(self, tmp_path):
        data = {
            "name": "custom-suite",
            "tags": ["simple", "test"],
            "items": [
                {"id": "d1", "question": "Q1?"},
                {"id": "d2", "question": "Q2?"},
            ],
        }
        f = tmp_path / "suite.yaml"
        f.write_text(yaml.dump(data), encoding="utf-8")
        suite = _load_file(f)
        assert suite.name == "custom-suite"
        assert suite.tags == ["simple", "test"]
        assert len(suite.items) == 2

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("", encoding="utf-8")
        suite = _load_file(f)
        assert suite.name == "empty"
        assert suite.items == []

    def test_none_data(self, tmp_path):
        f = tmp_path / "null.yaml"
        f.write_text("null", encoding="utf-8")
        suite = _load_file(f)
        assert suite.items == []

    def test_dict_with_benchmarks_key(self, tmp_path):
        data = {"benchmarks": [{"id": "b1", "question": "Q?"}]}
        f = tmp_path / "b.yaml"
        f.write_text(yaml.dump(data), encoding="utf-8")
        suite = _load_file(f)
        assert len(suite.items) == 1

    def test_unknown_type(self, tmp_path):
        f = tmp_path / "str.yaml"
        f.write_text("just a string", encoding="utf-8")
        suite = _load_file(f)
        assert suite.items == []


class TestLoadDirectory:
    def test_loads_yaml_files(self, temp_dir_with_yaml):
        suite = _load_directory(temp_dir_with_yaml)
        assert len(suite.items) == 2  # _template.yaml excluded
        assert suite.tags == []  # no tags set in test files

    def test_no_yaml_files_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _load_directory(tmp_path)

    def test_excludes_underscore_files(self, tmp_path):
        (tmp_path / "_hidden.yaml").write_text(yaml.dump([{"id": "h", "question": "Q?"}]), encoding="utf-8")
        (tmp_path / "visible.yaml").write_text(yaml.dump([{"id": "v", "question": "Q?"}]), encoding="utf-8")
        suite = _load_directory(tmp_path)
        assert len(suite.items) == 1
        assert suite.items[0].id == "v"


class TestLoadBenchmark:
    def test_load_file(self, temp_yaml_file):
        suite = load_benchmark(str(temp_yaml_file))
        assert len(suite.items) == 1

    def test_load_directory(self, temp_dir_with_yaml):
        suite = load_benchmark(str(temp_dir_with_yaml))
        assert len(suite.items) == 2

    def test_load_path_object(self, temp_yaml_file):
        suite = load_benchmark(temp_yaml_file)
        assert len(suite.items) == 1

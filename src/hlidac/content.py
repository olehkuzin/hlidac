"""Reading versioned content files from `data/` (YAML with no duplicate keys). No patient data."""

from pathlib import Path
from typing import Any

import yaml

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PyYAML keeps the last one silently)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: list[Any] = []
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate YAML key {key!r}", key_node.start_mark
                )
            seen.append(key)
        return super().construct_mapping(node, deep=deep)


def read_yaml(path: Path) -> Any:
    """The parsed file. Raises yaml.YAMLError (duplicate keys included) or OSError."""
    return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)

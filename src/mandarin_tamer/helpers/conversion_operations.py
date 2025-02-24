import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from opencc import OpenCC

from .conversion_config import ConversionConfig
from .file_conversion import FileConversion
from .replacement_by_dictionary import ReplacementUtils


@dataclass
class ConversionOperation:
    """Base class for conversion operations."""

    sentence: str
    indexes_to_protect: list[tuple[int, int]] | None = None
    config_name: str | None = None
    include_dict: dict | None = None
    exclude_list: list | None = None
    _phrase_trie: object = field(default=None, init=False)
    _opencc_cache: dict[str, str] = field(default_factory=dict, init=False)
    _current_opencc_config: str | None = field(default=None, init=False)
    _cached_converted: str | None = field(default=None, init=False)

    def __init__(
        self, sentence: str, indexes_to_protect: list[tuple[int, int]] | None = None, config_name: str | None = None
    ):
        self.sentence = sentence
        self.indexes_to_protect = indexes_to_protect or []
        self.config_name = config_name
        self._phrase_trie = None

    def apply_phrase_conversion(self, phrase_dict: dict) -> tuple[str, list[tuple[int, int]]]:
        """Apply phrase-level conversion with optimized performance."""
        if not phrase_dict or not any(phrase_dict.values()):
            return self.sentence, self.indexes_to_protect

        print(f"\nPhrase conversion timing breakdown [{self.config_name}]:")
        total_start = time.time()

        trie_start = time.time()
        if not self._phrase_trie:
            self._phrase_trie = ReplacementUtils.build_trie_from_dict(phrase_dict)
        print(f"  - [{self.config_name}] Trie building took: {time.time() - trie_start:.3f}s")

        match_start = time.time()
        matches = []
        protected_ranges = sorted(self.indexes_to_protect) if self.indexes_to_protect else []

        for start, end, replacement in self._phrase_trie.find_all_matches(self.sentence):
            if not any(r[0] <= start < r[1] or r[0] < end <= r[1] for r in protected_ranges):
                matches.append((start, end, replacement))

        matches.sort(reverse=True)
        print(f"  - [{self.config_name}] Finding matches took: {time.time() - match_start:.3f}s")
        print(f"  - [{self.config_name}] Number of matches found: {len(matches)}")

        replace_start = time.time()
        result = bytearray(self.sentence.encode("utf-8"))
        new_indexes = set(self.indexes_to_protect or [])

        for start, end, replacement in matches:
            byte_start = len(self.sentence[:start].encode("utf-8"))
            byte_end = len(self.sentence[:end].encode("utf-8"))
            replacement_bytes = replacement.encode("utf-8")

            result[byte_start:byte_end] = replacement_bytes
            new_indexes.add((start, start + len(replacement)))

        print(f"  - [{self.config_name}] Applying replacements took: {time.time() - replace_start:.3f}s")
        print(f"Total [{self.config_name}] phrase conversion took: {time.time() - total_start:.3f}s")

        return result.decode("utf-8"), list(new_indexes)

    def apply_one_to_many_conversion(
        self,
        mapping_dict: dict,
        use_improved_mode: bool = False,
        openai_func: Callable | None = None,
        opencc_config: str | None = None,
    ) -> str:
        """Apply one-to-many character conversion."""
        if not use_improved_mode and not opencc_config:
            msg = "Either improved mode or opencc_config must be specified"
            raise ValueError(msg)

        print(f"\nOne-to-many conversion timing breakdown [{self.config_name}]:")
        total_start = time.time()

        new_sentence = self.sentence
        indexes_to_protect = self.indexes_to_protect or []

        if use_improved_mode and openai_func:
            print(f"[{self.config_name}] Using improved mode with OpenAI:")
            chars_to_convert = [char for char in mapping_dict if char in new_sentence]
            print(f"  - [{self.config_name}] Found {len(chars_to_convert)} characters to convert")

            for i, char in enumerate(chars_to_convert, 1):
                char_start = time.time()
                replacement = openai_func(new_sentence, char, mapping_dict)
                new_sentence = new_sentence.replace(char, replacement)
                print(
                    f"  - [{self.config_name}] Character {i}/{len(chars_to_convert)} conversion took: {time.time() - char_start:.3f}s"
                )
        else:
            print(f"[{self.config_name}] Using OpenCC mode:")
            opencc_start = time.time()

            if opencc_config != self._current_opencc_config or self._cached_converted is None:
                cc = OpenCC(opencc_config)
                self._cached_converted = cc.convert(new_sentence)
                self._current_opencc_config = opencc_config
                print(f"  - [{opencc_config}] New OpenCC conversion took: {time.time() - opencc_start:.3f}s")
            else:
                print(f"  - [{opencc_config}] Using cached OpenCC conversion")

            replace_start = time.time()
            chars_to_convert = [char for char in mapping_dict if char in new_sentence]
            print(f"  - [{self.config_name}] Found {len(chars_to_convert)} characters to convert")

            for char in chars_to_convert:
                new_sentence = new_sentence.replace(char, self._cached_converted[new_sentence.index(char)])
            print(f"  - [{self.config_name}] Character replacement took: {time.time() - replace_start:.3f}s")

        protect_start = time.time()
        final_sentence = ReplacementUtils.revert_protected_indexes(self.sentence, new_sentence, indexes_to_protect)
        print(f"  - [{self.config_name}] Protecting indexes took: {time.time() - protect_start:.3f}s")
        print(f"Total [{self.config_name}] one-to-many conversion took: {time.time() - total_start:.3f}s")

        return final_sentence

    def apply_char_conversion(self, char_dict: dict) -> tuple[str, list[tuple[int, int]] | None]:
        """Apply character-level conversion."""
        chars_in_sentence = [char for char in char_dict if char in self.sentence]
        new_sentence = self.sentence
        indexes_to_protect = self.indexes_to_protect or []

        for char in chars_in_sentence:
            new_sentence = new_sentence.replace(char, char_dict[char])

        final_sentence = ReplacementUtils.revert_protected_indexes(self.sentence, new_sentence, indexes_to_protect)
        return final_sentence, indexes_to_protect


class DictionaryLoader:
    """Handles loading and merging of conversion dictionaries."""

    def __init__(self, base_path: Path | None = None):
        if base_path is None:
            # Use the package's conversion_dictionaries directory
            base_path = Path(__file__).parent.parent / "conversion_dictionaries"
        self.base_path = base_path

    def load_conversion_config(
        self,
        config: ConversionConfig,
        include_dicts: dict | None = None,
        exclude_lists: dict | None = None,
    ) -> dict[str, dict | None]:
        """Load all dictionaries for a conversion configuration."""
        include_dict = include_dicts.get(config.include_key) if include_dicts and config.include_key else None
        exclude_list = exclude_lists.get(config.include_key) if exclude_lists and config.include_key else None

        return {
            "char": self.merge_dicts(
                self.load_dict(config.sub_dir, config.char_file),
                include_dict,
                exclude_list,
            ),
            "phrase": self.merge_dicts(
                self.load_dict(config.sub_dir, config.phrase_file),
                include_dict,
                exclude_list,
            ),
            "one2many": self.merge_dicts(
                self.load_dict(config.sub_dir, config.one2many_file),
                include_dict,
                exclude_list,
            )
            if config.openai_func or config.opencc_config
            else None,
        }

    def merge_dicts(
        self,
        base_dict: dict,
        include_dict: dict | None = None,
        exclude_list: list | None = None,
    ) -> dict:
        """Merge dictionaries with include/exclude options."""
        merged_dict = base_dict.copy()
        if include_dict:
            merged_dict.update(include_dict)
        if exclude_list:
            for item in exclude_list:
                merged_dict.pop(item, None)
        return merged_dict

    def load_dict(self, sub_dir: str, filename: str) -> dict:
        """Load a dictionary from file."""
        path = self.base_path / sub_dir / filename if sub_dir else self.base_path / filename
        return FileConversion.json_to_dict(path)

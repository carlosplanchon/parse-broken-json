# parse_broken_json

_A small library to help parse broken or invalid JSON strings_

## Overview

`parse_broken_json` is a Python library designed to parse malformed or incomplete JSON strings that would typically fail with standard JSON parsers. It's particularly useful when dealing with:

- Truncated JSON responses from APIs or LLMs
- Streaming JSON data that arrives incomplete
- JSON with missing closing brackets or braces
- Partial JSON extractions from text

## Installation

### Install with uv:
```bash
uv add parse_broken_json
```

### Install with pip:
```bash
pip install parse_broken_json
```

## Usage

The library provides a single function:

```python
parse_broken_json(text: str, allowed_keys: list[str]) -> Any
```

**Parameters:**
- `text`: The JSON string to be parsed (can be broken or incomplete)
- `allowed_keys`: List of keys allowed in the JSON nodes (helps guide the parser)

**Returns:** The parsed Python object (dict, list, etc.)

### Example

```python
from parse_broken_json import parse_broken_json

# Parse incomplete JSON
broken_json = '{"name": "John", "age": 30, "address": {"city": "New'
allowed_keys = ["name", "age", "address", "city", "street"]

result = parse_broken_json(broken_json, allowed_keys)
print(result)  # {'name': 'John', 'age': 30, 'address': {'city': 'New'}}
```

## Implementation

Internally, this library uses the interpreter pattern to parse and recover from malformed JSON structures.

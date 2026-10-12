
from pathlib import Path

# Enter the full path of your original COBOL file here
INPUT_FILE = r"C:\COBOL\EMPLOYEE.CBL"


def remove_cobol_comments(line):
    """
    Remove fixed-format COBOL comment lines and inline comments.
    Columns 1-6: sequence area
    Column 7: indicator area
    """

    # Preserve the original line ending
    if line.endswith("\r\n"):
        content, ending = line[:-2], "\r\n"
    elif line.endswith("\n") or line.endswith("\r"):
        content, ending = line[:-1], line[-1:]
    else:
        content, ending = line, ""

    # Remove full-line comments and page-eject lines.
    # Column 7 is index 6 in Python (zero-based indexing).
    if len(content) >= 7 and content[6] in ("*", "/"):
        return None

    # Remove inline comments ( *>) outside quoted literals.
    # Start after column 7 so sequence/indicator fields are preserved.
    in_quote = None
    i = 7

    while i < len(content):
        char = content[i]

        if in_quote:
            if char == in_quote:
                # Doubled quotes represent a quote inside a literal.
                if i + 1 < len(content) and content[i + 1] == in_quote:
                    i += 2
                    continue
                in_quote = None
        elif char in ("'", '"'):
            in_quote = char
        elif char == "*" and i + 1 < len(content):
            if content[i + 1] == ">":
                content = content[:i]
                break

        i += 1

    return content + ending


def main():
    source = Path(INPUT_FILE).expanduser()

    if not source.is_file():
        print(f"Error: COBOL file not found: {source}")
        return

    # Read without changing line endings.
    with source.open("r", encoding="utf-8", newline="") as file:
        lines = file.readlines()

    cleaned_lines = []

    for line in lines:
        cleaned = remove_cobol_comments(line)

        # None means the entire comment/page-eject line is removed.
        if cleaned is not None:
            cleaned_lines.append(cleaned)

    # Save in the directory from which this script is run.
    output_dir = Path.cwd()
    output_file = output_dir / f"{source.stem}_cleaned{source.suffix}"

    with output_file.open("w", encoding="utf-8", newline="") as file:
        file.writelines(cleaned_lines)

    print("Comment removal completed.")
    print(f"Original file : {source}")
    print(f"Cleaned copy  : {output_file}")


if __name__ == "__main__":
    main()

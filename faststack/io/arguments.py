"""Parse persisted external-tool arguments into shell-free argv elements."""

import os
import shlex


def _split_windows_command_line(command: str) -> list[str]:
    """Apply the Windows C-runtime quote/backslash rules without a shell."""
    args: list[str] = []
    index = 0
    length = len(command)

    while index < length:
        while index < length and command[index] in " \t":
            index += 1
        if index >= length:
            break

        argument: list[str] = []
        in_quotes = False
        started = False
        while index < length:
            char = command[index]
            if char in " \t" and not in_quotes:
                break

            if char == "\\":
                slash_start = index
                while index < length and command[index] == "\\":
                    index += 1
                slash_count = index - slash_start
                if index < length and command[index] == '"':
                    argument.extend("\\" * (slash_count // 2))
                    started = True
                    if slash_count % 2:
                        argument.append('"')
                    else:
                        in_quotes = not in_quotes
                    index += 1
                else:
                    argument.extend("\\" * slash_count)
                    started = True
                continue

            if char == '"':
                in_quotes = not in_quotes
                started = True
                index += 1
                continue

            argument.append(char)
            started = True
            index += 1

        if in_quotes:
            raise ValueError("No closing quotation")
        if started:
            args.append("".join(argument))

        while index < length and command[index] in " \t":
            index += 1

    return args


def parse_external_arguments(value: str, *, windows: bool | None = None) -> list[str]:
    """Return exact argv elements for a persisted user argument string."""
    if not isinstance(value, str):
        raise TypeError("external arguments must be a string")
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return _split_windows_command_line(value)
    return shlex.split(value, posix=True)


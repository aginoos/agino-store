# Server tests

Standard library plus `python3-yaml` and `openssl` only; everything happens in temporary
directories and nothing touches the network. Run from the repository root:

    python3 -I -m unittest discover -s tests/server -t .

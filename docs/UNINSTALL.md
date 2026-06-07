# Uninstall And Cleanup

Uninstall has two different meanings:

| Goal | Command |
|---|---|
| Stop the local 8mem server | `8mem stop` |
| Remove the Python package | `pipx uninstall 8mem` or `pip uninstall 8mem` |
| Remove OpenClaw integration only | `8mem uninstall --mode openclaw` |
| Delete local 8mem memory/config | `rm -rf ~/.8mem` |

## Stop 8mem

```bash
8mem stop
```

## Remove Package Installed With pipx

```bash
pipx uninstall 8mem
```

## Remove Package Installed With pip

```bash
pip uninstall 8mem
```

## Keep Or Delete Local Memory

By default, uninstalling the package does not delete:

```text
~/.8mem
```

This is intentional. Apps should not delete user memory automatically.

If you want to permanently delete local 8mem memory and config:

```bash
rm -rf ~/.8mem
```

Only run that command if you are sure.

## Remove OpenClaw Integration

If you connected OpenClaw and want to remove only the 8mem integration:

```bash
8mem uninstall --mode openclaw
```

This does not uninstall OpenClaw itself.

## Clean Reinstall

Keep memory:

```bash
8mem stop
pipx uninstall 8mem
pipx install 8mem
8mem setup
8mem start
```

Delete everything and start fresh:

```bash
8mem stop
pipx uninstall 8mem
rm -rf ~/.8mem
pipx install 8mem
8mem setup
8mem start
```

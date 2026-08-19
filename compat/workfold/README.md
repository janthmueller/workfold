# Workfold has become Wuf

Workfold continues as [Wuf](https://pypi.org/project/wuf/): the local CLI that
folds Git and filesystem timestamp footprints into a representative weekly
activity view.

This distribution is an installation bridge. Installing or upgrading
`workfold` installs the maintained `wuf` distribution, which provides the
canonical `wuf` command and a compatible `workfold` command:

```bash
pip install --upgrade workfold
wuf --help
```

New installations should use `pip install wuf` directly. Documentation and
development continue at <https://github.com/janthmueller/wuf>.

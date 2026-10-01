# Releasing

`titiler/pycsw/__init__.py` holds the version. Nothing derives it from git, so
that file is the source of truth and the release tag has to agree with it.

## Cutting a release

1. Bump `__version__` in `titiler/pycsw/__init__.py`.
2. Merge to `main`.
3. Publish a GitHub Release whose tag is the version, with or without a leading
   `v` (`0.1.0` and `v0.1.0` both work).

That triggers:

| workflow | publishes |
|---|---|
| `publish-pypi.yml` | `titiler-pycsw` to PyPI |
| `publish-docker.yml` | `ghcr.io/developmentseed/titiler-pycsw:<version>` and `:latest` |

`publish-docker.yml` also runs on every push to `main`, refreshing `:latest`.

The PyPI job builds first and compares the release tag against the version in
the built wheel, failing before it uploads anything if they disagree.

## One-time setup

**PyPI needs no API token.** Publishing uses trusted publishing over OIDC, so
nothing is stored in the repository. On PyPI, add a pending publisher under
*Your projects → Publishing*:

| field | value |
|---|---|
| PyPI project name | `titiler-pycsw` |
| Owner | `developmentseed` |
| Repository name | `titiler-pycsw` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `pypi-release` |

Then create a `pypi-release` environment in the repository settings. Restrict it
to protected branches and tags if you want a second pair of eyes before an
upload.

**GHCR needs no secret either** — `GITHUB_TOKEN` with `packages: write` is
enough. The first push creates the package, owned by the repository and private;
make it public under the package's settings if that is what you want.

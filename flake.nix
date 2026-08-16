{
  description = "workfold Python CLI";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      project = builtins.fromTOML (builtins.readFile ./pyproject.toml);
    in
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;
        pythonPackages = pkgs.python312Packages;
        projectSource = pkgs.lib.cleanSourceWith {
          src = ./.;
          filter = path: _type:
            let
              name = builtins.baseNameOf path;
              sourceRoot = toString ./.;
              relativePath = pkgs.lib.removePrefix "${sourceRoot}/" (toString path);
              generatedNames = [
                ".agents"
                ".astro"
                ".cache"
                ".codex"
                ".coverage"
                ".direnv"
                ".git"
                ".hypothesis"
                ".mypy_cache"
                ".nox"
                ".pnpm-home"
                ".pytest_cache"
                ".pyright"
                ".ruff_cache"
                ".tox"
                ".venv"
                "__pycache__"
                "build"
                "coverage.xml"
                "dist"
                "htmlcov"
                "node_modules"
                "result"
                "workfold.egg-info"
                "workfold.spec"
              ];
            in
            pkgs.lib.cleanSourceFilter path _type
            && !(builtins.elem name generatedNames)
            && relativePath != "benchmarks/results";
        };

        workfold = pythonPackages.buildPythonPackage {
          pname = project.project.name;
          version = project.project.version;
          pyproject = true;
          src = projectSource;

          build-system = with pythonPackages; [ setuptools ];
          dependencies = with pythonPackages; [
            pathspec
            rich
            tzlocal
          ];
          makeWrapperArgs = [
            "--prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.git ]}"
          ];
          nativeCheckInputs = [ pkgs.git pythonPackages.pytestCheckHook ];
          pythonImportsCheck = [ "workfold" ];

          meta = with pkgs.lib; {
            inherit (project.project) description;
            homepage = project.project.urls.homepage;
            license = licenses.mit;
            mainProgram = "workfold";
            platforms = platforms.unix;
          };
        };

        docsCommand = name: command: pkgs.writeShellApplication {
          name = "workfold-docs-${name}";
          runtimeInputs = [ pkgs.nodejs_24 pkgs.pnpm ];
          text = ''
            export PNPM_HOME="$PWD/.pnpm-home"
            export PATH="$PNPM_HOME:$PATH"
            if [ ! -d docs/node_modules ]; then
              pnpm --dir docs install --frozen-lockfile
            fi
            pnpm --dir docs ${command}
          '';
        };

        docs-install = pkgs.writeShellApplication {
          name = "workfold-docs-install";
          runtimeInputs = [ pkgs.nodejs_24 pkgs.pnpm ];
          text = ''
            export PNPM_HOME="$PWD/.pnpm-home"
            export PATH="$PNPM_HOME:$PATH"
            pnpm --dir docs install --frozen-lockfile
          '';
        };
        docs-dev = docsCommand "dev" "dev";
        docs-check = docsCommand "check" "check";
        docs-build = docsCommand "build" "build";
        app = drv: description:
          (flake-utils.lib.mkApp { inherit drv; }) // {
            meta = { inherit description; };
          };
      in
      {
        packages.default = workfold;

        apps = {
          default = app workfold project.project.description;
          docs-install = app docs-install "Install Workfold documentation dependencies";
          docs-dev = app docs-dev "Run the Workfold documentation development server";
          docs-check = app docs-check "Validate the Workfold documentation site";
          docs-build = app docs-build "Build the Workfold documentation site";
        };

        checks.default = workfold;

        devShells.default = pkgs.mkShell {
          packages = [
            (python.withPackages (ps: [
              ps.build
              ps.pyinstaller
              ps.twine
              ps.uv
            ]))
            pkgs.actionlint
            pkgs.git
            pkgs.nodejs_24
            pkgs.pnpm
            pkgs.ruff
          ];

          shellHook = ''
            unset PYTHONPATH
            export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
            export PNPM_HOME="$PWD/.pnpm-home"
            export PATH="$PNPM_HOME:$PATH"
          '';
        };
      });
}

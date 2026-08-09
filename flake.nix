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

        workfold = pythonPackages.buildPythonPackage {
          pname = project.project.name;
          version = project.project.version;
          pyproject = true;
          src = ./.;

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
            platforms = platforms.unix ++ platforms.windows;
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
      in
      {
        packages.default = workfold;

        apps = {
          default = flake-utils.lib.mkApp { drv = workfold; };
          docs-install = {
            type = "app";
            program = "${docs-install}/bin/workfold-docs-install";
          };
          docs-dev = {
            type = "app";
            program = "${docs-dev}/bin/workfold-docs-dev";
          };
          docs-check = {
            type = "app";
            program = "${docs-check}/bin/workfold-docs-check";
          };
          docs-build = {
            type = "app";
            program = "${docs-build}/bin/workfold-docs-build";
          };
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

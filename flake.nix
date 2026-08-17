{
  description = "Minimal lnurlcash (LUD-25, Lightning bearer assets) mint - LUD-03/LUD-06 only.";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        rec {
          lnurl-mint = pkgs.callPackage ./nix/package.nix { src = self; };
          default = lnurl-mint;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.lnurl-mint}/bin/lnurl-mint";
        };
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          runtimeDeps = self.packages.${system}.lnurl-mint.passthru.runtimeDeps;

          # nixpkgs ships black 26, whose new hugging style would reformat
          # the tree and fight CI - the project pins black 24 (see
          # pyproject), so the shell provides the pinned version, same
          # version+hash as uv.lock
          black = pkgs.python3Packages.buildPythonPackage rec {
            pname = "black";
            version = "24.10.0";
            pyproject = true;
            src = pkgs.fetchPypi {
              inherit pname version;
              hash = "sha256-hG6mTJev47xne3YXh5k75JkYEOzHpKk3gW3Wvd7cSHU=";
            };
            build-system = with pkgs.python3Packages; [
              hatchling
              hatch-fancy-pypi-readme
              hatch-vcs
            ];
            env.SETUPTOOLS_SCM_PRETEND_VERSION = version;
            dependencies = with pkgs.python3Packages; [
              click
              mypy-extensions
              packaging
              pathspec
              platformdirs
              tomli
            ];
            doCheck = false;
          };
        in
        {
          # python with the full runtime closure + test/lint tooling; pytest
          # runs against the working tree, so this replaces the fragile uv
          # venv entirely (coincurve comes prebuilt from nixpkgs)
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (
                ps:
                runtimeDeps ps
                ++ [
                  ps.pytest
                  ps.mypy
                ]
              ))
              black
              pkgs.ruff
            ];
          };
        }
      );

      nixosModules = {
        lnurl-mint = import ./nix/module.nix { inherit self; };
        default = self.nixosModules.lnurl-mint;
      };

      checks = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          # the package build itself runs the full pytest suite (see
          # package.nix's checkPhase)
          inherit (self.packages.${system}) lnurl-mint;

          # cheap evaluation check: the module merges and produces a sane
          # unit without building a whole NixOS system
          module-eval =
            let
              eval = nixpkgs.lib.nixosSystem {
                inherit system;
                modules = [
                  self.nixosModules.lnurl-mint
                  {
                    services.lnurl-mint = {
                      enable = true;
                      settings = {
                        BASE_URL = "https://mint.example";
                        VERIFY_ENABLED = false;
                        BASE_FEE_MSAT = 1000;
                      };
                    };
                    system.stateVersion = "25.11";
                  }
                ];
              };
            in
            pkgs.runCommand "lnurl-mint-module-eval" { } ''
              unit=${pkgs.writeText "unit.json" (builtins.toJSON {
                inherit (eval.config.systemd.services.lnurl-mint) serviceConfig environment;
              })}
              grep -q '"StateDirectory":"lnurl-mint"' $unit
              grep -q '"DynamicUser":true' $unit
              grep -q '"BASE_URL":"https://mint.example"' $unit
              grep -q '"VERIFY_ENABLED":"false"' $unit
              grep -q '/bin/lnurl-mint --host 127.0.0.1 --port 8111' $unit
              touch $out
            '';

          # the real proof: a VM booting the module end-to-end
          vm-smoke = pkgs.testers.nixosTest {
            name = "lnurl-mint";
            nodes.machine =
              { ... }:
              {
                imports = [ self.nixosModules.lnurl-mint ];
                services.lnurl-mint = {
                  enable = true;
                  settings.BASE_URL = "http://localhost:8111";
                };
              };
            testScript = ''
              machine.wait_for_unit("lnurl-mint.service")
              machine.wait_for_open_port(8111)
              # LUD-06 payRequest works without a funding source configured
              machine.succeed("curl -sf http://localhost:8111/p | grep -q withdrawLink")
              # the mutating callback correctly reports the missing funding source
              machine.succeed("curl -sf 'http://localhost:8111/p/cb?amount=50000' | grep -q 'not configured'")
              # the frontend one-pager renders
              machine.succeed("curl -sf http://localhost:8111/ | grep -q lnurl-mint")
            '';
          };
        }
      );
    };
}

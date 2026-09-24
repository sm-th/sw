{
  description = "sw: tools and site kit for Smith Wiki research repositories";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      # nixpkgs 26.11 dropped x86_64-darwin.
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };
      packagesFor =
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet =
            (pkgs.callPackage pyproject-nix.build.packages {
              python = pkgs.python3;
            }).overrideScope
              (
                nixpkgs.lib.composeManyExtensions [
                  pyproject-build-systems.overlays.wheel
                  overlay
                ]
              );
          pythonEnv = pythonSet.mkVirtualEnv "sw-env" workspace.deps.default;
          nodejs = pkgs.nodejs_22;
          nodeModules = pkgs.importNpmLock.buildNodeModules {
            npmRoot = ./site;
            inherit nodejs;
          };
          runtime = [
            pkgs.awscli2
            pkgs.bash
            pkgs.coreutils
            pkgs.findutils
            pkgs.gh
            pkgs.git
            pkgs.gnugrep
            pkgs.gnused
            pkgs.jq
            pkgs.secretspec
            nodejs
            pythonEnv
          ];
          sw = pkgs.stdenvNoCC.mkDerivation {
            pname = "sw";
            version = "0";
            src = nixpkgs.lib.fileset.toSource {
              root = ./.;
              fileset = nixpkgs.lib.fileset.unions [
                ./bin
                ./lib
                ./site
                ./GUIDE.md
                ./secretspec.toml
              ];
            };
            nativeBuildInputs = [ pkgs.makeWrapper ];
            buildInputs = [ pkgs.bash ];
            installPhase = ''
              mkdir -p $out/share/sw $out/bin
              cp -R bin lib site GUIDE.md secretspec.toml $out/share/sw/
              ln -s ${nodeModules}/node_modules $out/share/sw/site/node_modules
              patchShebangs $out/share/sw/bin
              makeWrapper $out/share/sw/bin/sw $out/bin/sw --prefix PATH : ${nixpkgs.lib.makeBinPath runtime}
            '';
          };
        in
        {
          inherit pkgs pythonEnv sw;
        };
    in
    {
      packages = forAllSystems (
        system:
        let
          p = packagesFor system;
        in
        {
          inherit (p) sw;
          default = p.sw;
        }
      );

      devShells = forAllSystems (
        system:
        let
          p = packagesFor system;
        in
        {
          default = p.pkgs.mkShellNoCC {
            packages = [
              p.sw
              p.pkgs.uv
            ];
          };
        }
      );
    };
}

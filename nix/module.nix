{ self }:

{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.lnurl-mint;
  inherit (lib)
    mkEnableOption
    mkOption
    mkIf
    types
    ;

  toEnvValue = v: if builtins.isBool v then (if v then "true" else "false") else toString v;
in
{
  options.services.lnurl-mint = {
    enable = mkEnableOption "lnurl-mint, a minimal lnurlcash (LUD-25) Lightning bearer-note mint";

    package = lib.mkPackageOption self.packages.${pkgs.stdenv.hostPlatform.system} "lnurl-mint" { };

    host = lib.mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = ''
        Address uvicorn binds to. Loopback by default - front it with a
        reverse proxy for TLS when reachable from the internet (or point a
        Tor HiddenServiceDir at it and set ONION_URL via settings).
      '';
    };

    port = lib.mkOption {
      type = types.port;
      default = 8111;
      description = "Port uvicorn listens on.";
    };

    dataDir = lib.mkOption {
      type = types.path;
      default = "/var/lib/lnurl-mint";
      description = ''
        Directory for mint.db (sqlite) and the mint/error logs - sqlite's
        journal/WAL files live here too, so the mint needs write access to
        the whole directory, not just the db file. Created via systemd's
        StateDirectory (mode 0750) when left at the default.
      '';
    };

    settings = lib.mkOption {
      type = types.attrsOf (types.oneOf [
        types.str
        types.int
        types.bool
      ]);
      default = { };
      example = {
        BASE_URL = "https://mint.example.com";
        FUNDINGSOURCE_BACKEND = "cln";
        FUNDINGSOURCE_URL = "https://localhost:3010";
        BASE_FEE_MSAT = 1000;
        VERIFY_ENABLED = false;
      };
      description = ''
        Environment variables for the mint (see upstream .env.example for
        the full list), rendered 1:1 as KEY=value. Non-secret values only:
        secrets (FUNDINGSOURCE_MACAROON, FUNDINGSOURCE_RUNE) belong in
        environmentFiles so they stay out of the world-readable nix store.
      '';
    };

    environmentFiles = lib.mkOption {
      type = types.listOf types.path;
      default = [ ];
      example = [ "/run/secrets/lnurl-mint" ];
      description = ''
        systemd EnvironmentFile(s) for the secret half of the config -
        FUNDINGSOURCE_MACAROON / FUNDINGSOURCE_RUNE, and BASE_URL if you'd
        rather keep it out of the store too. Loaded after `settings`, so
        values here win on conflict.
      '';
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.settings ? BASE_URL || cfg.environmentFiles != [ ];
        message = ''
          services.lnurl-mint: BASE_URL must be set somewhere (settings or an
          environmentFile) - the mint refuses to start without it, since its
          callback URLs must never be derived from a request's Host header.
        '';
      }
    ];

    systemd.services.lnurl-mint = {
      description = "lnurl-mint lnurlcash bearer-note mint";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      environment = {
        DATABASE_PATH = "${cfg.dataDir}/mint.db";
      }
      // (lib.mapAttrs (_: toEnvValue) cfg.settings);

      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} --host ${cfg.host} --port ${toString cfg.port}";
        EnvironmentFile = cfg.environmentFiles;
        Restart = "on-failure";
        RestartSec = 5;

        DynamicUser = true;
        ReadWritePaths = [ cfg.dataDir ];
        # the mint handles bearer secrets and node credentials - lock it down
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        RestrictNamespaces = true;
        RestrictRealtime = true;
        LockPersonality = true;
        # inbound HTTP + outbound HTTPS to the funding source, nothing else
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
      }
      // lib.optionalAttrs (cfg.dataDir == "/var/lib/lnurl-mint") {
        StateDirectory = "lnurl-mint";
        StateDirectoryMode = "0750";
      };
    };
  };
}

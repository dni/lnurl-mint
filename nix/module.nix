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

    verifyEnabled = lib.mkOption {
      type = types.bool;
      default = true;
      description = ''
        VERIFY_ENABLED (LUD-21): serve /verify/{payment_hash} and advertise
        it in /p/cb and melt responses, so wallets without their own node can
        poll settlement. Note the settled mint response's preimage IS the
        bearer note's spend secret, served to any holder of the payment hash
        (which travels inside the invoice) - see the README's "The observer
        race, plainly". false disables the endpoint entirely (404).
      '';
    };

    fundingSource = {
      backend = lib.mkOption {
        type = types.nullOr (
          types.enum [
            "lnd"
            "cln"
          ]
        );
        default = null;
        description = ''
          FUNDINGSOURCE_BACKEND: the Lightning node implementation funding
          this mint. null leaves minting/melting/offline-verification
          unavailable (rotate/split/merge still work). The credential goes
          in environmentFiles (FUNDINGSOURCE_MACAROON / FUNDINGSOURCE_RUNE),
          never in the nix store - scope it as the README documents.
        '';
      };

      url = lib.mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "https://localhost:3010";
        description = "FUNDINGSOURCE_URL: the node's REST endpoint (clnrest or lnd REST).";
      };

      certPath = lib.mkOption {
        type = types.nullOr types.path;
        default = null;
        description = ''
          FUNDINGSOURCE_CERT_PATH: a self-signed TLS cert to verify the
          funding source against - both lnd's and cln's REST APIs are
          commonly self-signed. Leave null if it's fronted by a reverse
          proxy with a real certificate. Note: paths are copied into the
          nix store, which is fine for a public cert (it authenticates the
          node TO the mint, it grants no access itself).
        '';
      };
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
        BASE_FEE_MSAT = 1000;
        ONION_URL = "http://<v3-address>.onion";
      };
      description = ''
        Environment variables for the mint (see upstream .env.example for
        the full list), rendered 1:1 as KEY=value - the escape hatch for
        anything without a dedicated option (verifyEnabled, fundingSource,
        host/port, dataDir). Dedicated options win over a raw key naming
        the same variable. Non-secret values only: secrets
        (FUNDINGSOURCE_MACAROON, FUNDINGSOURCE_RUNE) belong in
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
      {
        assertion = (cfg.fundingSource.backend == null) == (cfg.fundingSource.url == null);
        message = ''
          services.lnurl-mint: fundingSource.backend and fundingSource.url
          must be set together (or neither).
        '';
      }
    ];

    systemd.services.lnurl-mint = {
      description = "lnurl-mint lnurlcash bearer-note mint";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      # dedicated options (verifyEnabled, fundingSource) win over a raw
      # `settings` key naming the same variable; systemd EnvironmentFiles
      # load after all of this and win over both
      environment =
        {
          DATABASE_PATH = "${cfg.dataDir}/mint.db";
        }
        // (lib.mapAttrs (_: toEnvValue) cfg.settings)
        // {
          VERIFY_ENABLED = toEnvValue cfg.verifyEnabled;
        }
        // lib.optionalAttrs (cfg.fundingSource.backend != null) {
          FUNDINGSOURCE_BACKEND = cfg.fundingSource.backend;
        }
        // lib.optionalAttrs (cfg.fundingSource.url != null) {
          FUNDINGSOURCE_URL = cfg.fundingSource.url;
        }
        // lib.optionalAttrs (cfg.fundingSource.certPath != null) {
          FUNDINGSOURCE_CERT_PATH = toString cfg.fundingSource.certPath;
        };

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

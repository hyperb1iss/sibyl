# Sibyl Runner - Distributed agent execution daemon
# https://github.com/hyperb1iss/sibyl
#
# Install: brew install hyperb1iss/sibyl/sibyl-runner
# Start:   brew services start sibyl-runner

class SibylRunner < Formula
  include Language::Python::Virtualenv

  desc "Distributed agent runner daemon for Sibyl - AI agent orchestration"
  homepage "https://github.com/hyperb1iss/sibyl"
  url "https://files.pythonhosted.org/packages/source/s/sibyl-runner/sibyl_runner-0.2.0.tar.gz"
  sha256 "PLACEHOLDER_SHA256"
  license "Apache-2.0"
  head "https://github.com/hyperb1iss/sibyl.git", branch: "main"

  depends_on "python@3.13"
  depends_on "git"

  # Runtime dependencies from sibyl-runner's pyproject.toml
  resource "sibyl-core" do
    url "https://files.pythonhosted.org/packages/source/s/sibyl-core/sibyl_core-0.2.0.tar.gz"
    sha256 "PLACEHOLDER_SHA256"
  end

  resource "typer" do
    url "https://files.pythonhosted.org/packages/source/t/typer/typer-0.20.0.tar.gz"
    sha256 "PLACEHOLDER_SHA256"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-13.9.4.tar.gz"
    sha256 "PLACEHOLDER_SHA256"
  end

  resource "websockets" do
    url "https://files.pythonhosted.org/packages/source/w/websockets/websockets-15.0.tar.gz"
    sha256 "PLACEHOLDER_SHA256"
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/source/h/httpx/httpx-0.27.2.tar.gz"
    sha256 "PLACEHOLDER_SHA256"
  end

  resource "pyjwt" do
    url "https://files.pythonhosted.org/packages/source/p/pyjwt/PyJWT-2.10.1.tar.gz"
    sha256 "PLACEHOLDER_SHA256"
  end

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/source/p/pyyaml/PyYAML-6.0.2.tar.gz"
    sha256 "PLACEHOLDER_SHA256"
  end

  resource "anyio" do
    url "https://files.pythonhosted.org/packages/source/a/anyio/anyio-4.8.0.tar.gz"
    sha256 "PLACEHOLDER_SHA256"
  end

  def install
    virtualenv_install_with_resources
  end

  def post_install
    # Create config directory
    (var/"sibyl").mkpath
    (var/"sibyl/worktrees").mkpath
  end

  # Launchd plist for brew services
  service do
    run [opt_bin/"sibyl-runner", "run"]
    keep_alive true
    working_dir var/"sibyl"
    log_path var/"log/sibyl-runner.log"
    error_log_path var/"log/sibyl-runner.log"
    environment_variables SIBYL_WORKTREE_BASE: var/"sibyl/worktrees",
                          SIBYL_CONFIG_DIR: etc/"sibyl"
  end

  def caveats
    <<~EOS
      Sibyl Runner has been installed!

      Before starting the runner, you need to register it with your Sibyl server:

        sibyl-runner register --server https://sibyl.example.com --name "my-runner"

      This creates the config file at:
        #{etc}/sibyl/runner.yaml

      Then start the service:
        brew services start sibyl-runner

      Or run manually:
        sibyl-runner run

      Worktrees will be created at:
        #{var}/sibyl/worktrees

      Logs are available at:
        #{var}/log/sibyl-runner.log
    EOS
  end

  test do
    assert_match "sibyl-runner", shell_output("#{bin}/sibyl-runner --version")
  end
end

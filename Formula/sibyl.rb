# Sibyl CLI - Client for Sibyl knowledge graph
# https://github.com/hyperb1iss/sibyl
#
# Install: brew install hyperb1iss/sibyl/sibyl

class Sibyl < Formula
  include Language::Python::Virtualenv

  desc "CLI for Sibyl - Collective Intelligence Runtime for AI agents"
  homepage "https://github.com/hyperb1iss/sibyl"
  url "https://files.pythonhosted.org/packages/source/s/sibyl-dev/sibyl_dev-0.2.0.tar.gz"
  sha256 "PLACEHOLDER_SHA256"
  license "Apache-2.0"
  head "https://github.com/hyperb1iss/sibyl.git", branch: "main"

  depends_on "python@3.13"

  # Runtime dependencies from sibyl-dev's pyproject.toml
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

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/source/h/httpx/httpx-0.27.2.tar.gz"
    sha256 "PLACEHOLDER_SHA256"
  end

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/source/p/pyyaml/PyYAML-6.0.2.tar.gz"
    sha256 "PLACEHOLDER_SHA256"
  end

  def install
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      Sibyl CLI has been installed!

      Configure your Sibyl server:
        export SIBYL_API_URL=https://sibyl.example.com

      Or create a config file at:
        ~/.config/sibyl/config.yaml

      Get started:
        sibyl --help
        sibyl health
        sibyl search "your query"
    EOS
  end

  test do
    assert_match "sibyl-dev version", shell_output("#{bin}/sibyl version")
  end
end

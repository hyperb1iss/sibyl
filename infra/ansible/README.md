# hyperb1iss.sibyl

Ansible collection for deploying Sibyl on a single host. It carries one role, `sibyl`, which
installs Docker, renders the compose stack and its environment, and runs it under a systemd unit.

Install it straight from this repository, pinned to a release tag:

```yaml
# requirements.yml
collections:
  - name: https://github.com/hyperb1iss/sibyl.git#/infra/ansible
    type: git
    version: v1.4.9
```

```bash
ansible-galaxy collection install -r requirements.yml
```

Then apply `hyperb1iss.sibyl.sibyl` from a play. The tag decides both the role and the image
versions it deploys, so upgrading is a one-line bump of `version`. The
[single-host deployment guide](../../docs/deployment/ansible.md) covers variables, secrets, and
operations.

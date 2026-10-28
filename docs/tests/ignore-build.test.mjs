import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, copyFileSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';

test('build filter handles first deployment, docs changes, unrelated changes and missing history', () => {
  const root = mkdtempSync(join(tmpdir(), 'docs-build-test-'));
  const git = (...args) => {
    const result = spawnSync('git', args, {cwd: root, encoding: 'utf8'});
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  };
  try {
    mkdirSync(join(root, 'docs', 'scripts'), {recursive: true});
    copyFileSync(new URL('../scripts/ignore-build.mjs', import.meta.url), join(root, 'docs', 'scripts', 'ignore-build.mjs'));
    git('init');
    git('config', 'user.email', 'test@example.invalid');
    git('config', 'user.name', 'Local test');
    git('add', '.');
    git('commit', '-m', 'initial');
    const previous = git('rev-parse', 'HEAD');
    const run = sha => spawnSync(process.execPath, ['scripts/ignore-build.mjs'], {
      cwd: join(root, 'docs'), env: {...process.env, VERCEL_GIT_PREVIOUS_SHA: sha},
    }).status;
    assert.equal(run(''), 1);
    assert.equal(run(previous), 0);
    writeFileSync(join(root, 'app.txt'), 'unrelated');
    git('add', '.'); git('commit', '-m', 'app');
    assert.equal(run(previous), 0);
    writeFileSync(join(root, 'docs', 'page.md'), 'changed');
    git('add', '.'); git('commit', '-m', 'docs');
    assert.equal(run(previous), 1);
    assert.equal(run('deadbeef'), 1);
  } finally {
    rmSync(root, {recursive: true, force: true});
  }
});

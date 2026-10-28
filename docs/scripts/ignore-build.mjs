import {spawnSync} from 'node:child_process';

// Vercel: 0 skips the build; 1 builds. Missing history always builds.
const previous = process.env.VERCEL_GIT_PREVIOUS_SHA;
if (!previous) process.exit(1);
const result = spawnSync('git', ['diff', '--quiet', previous, 'HEAD', '--', '.'], {
  cwd: new URL('../', import.meta.url), stdio: 'inherit',
});
process.exit(result.status === 0 ? 0 : 1);

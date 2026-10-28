import {next} from '@vercel/functions';
import {authorize} from './scripts/auth.mjs';

// No exclusions: HTML, JS chunks, images and unknown paths all require auth.
export const config = {matcher: '/:path*'};

export default async function middleware(request) {
  const denied = await authorize(request, process.env);
  return denied || next({headers: {
    'Cache-Control': 'private, no-store',
    'X-Robots-Tag': 'noindex, nofollow, noarchive',
  }});
}

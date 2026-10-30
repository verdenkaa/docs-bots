import sections from './scripts/sections.cjs';

export default {
  title: 'Документация проекта',
  tagline: 'Тестовая документация',
  url: process.env.DOCS_SITE_URL || 'https://example.vercel.app',
  baseUrl: '/',
  trailingSlash: true,
  onBrokenLinks: 'throw',
  i18n: {defaultLocale: 'ru', locales: ['ru']},
  markdown: {mermaid: true},
  themes: [
    '@docusaurus/theme-mermaid',
    ['@easyops-cn/docusaurus-search-local', {
      hashed: 'filename',
      language: ['ru', 'en'],
      docsDir: 'documents',
      docsRouteBasePath: '/',
      indexBlog: false,
      indexPages: false,
      highlightSearchTermsOnTargetPage: true,
    }],
  ],
  presets: [['classic', {
    docs: {path: 'documents', routeBasePath: '/', sidebarPath: './sidebars.js'},
    blog: false,
    pages: false,
    sitemap: false,
  }]],
  themeConfig: {
    colorMode: {defaultMode: 'dark', respectPrefersColorScheme: false},
    navbar: {title: 'Документация', items: sections.navbarItems},
    mermaid: {theme: {light: 'neutral', dark: 'dark'}},
  },
};

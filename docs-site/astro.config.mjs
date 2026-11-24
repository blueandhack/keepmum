// keepmum's docs, served at keepmum.app/docs. `npm run build` writes them into site/docs/, which the
// Site workflow publishes with the rest of site/ (GitHub Pages). site/docs/ is build output: not
// committed. The pages are Markdown in src/content/docs/; the sidebar below is their order.
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

const repo = 'https://github.com/blueandhack/keepmum';

export default defineConfig({
	site: 'https://keepmum.app',
	base: '/docs',
	outDir: '../site/docs',
	trailingSlash: 'always',
	// No telemetry from the build either.
	devToolbar: { enabled: false },
	integrations: [
		starlight({
			title: 'keepmum docs',
			description:
				'keepmum keeps secrets and personal data on your Mac while you use Claude Code, Codex, the Antigravity CLI, Gemini CLI, pi or opencode.',
			logo: { src: './src/assets/logo.svg', alt: 'keepmum' },
			favicon: '/favicon.svg',
			social: [{ icon: 'github', label: 'GitHub', href: repo }],
			// keepmum.app's home in the header, beside GitHub: the logo goes to the docs' front page.
			components: { SocialIcons: './src/components/SocialIcons.astro' },
			editLink: { baseUrl: `${repo}/edit/main/docs-site/` },
			lastUpdated: true,
			customCss: ['./src/styles/keepmum.css'],
			head: [
				// The same analytics as the rest of keepmum.app, off for Global Privacy Control and Do
				// Not Track (site/analytics.js, keepmum.app/privacy).
				{ tag: 'script', attrs: { src: '/analytics.js', defer: true } },
				{ tag: 'meta', attrs: { property: 'og:image', content: 'https://keepmum.app/og.png' } },
			],
			sidebar: [
				{ label: 'Overview', link: '/' },
				{
					label: 'Get started',
					items: [
						'start/install',
						'start/agents',
						'start/background',
						'start/gateways',
						'start/mac-app',
					],
				},
				{
					label: 'Masking',
					items: [
						'masking/what-gets-masked',
						'masking/rules-only',
						'masking/observe',
						'masking/exclude',
						'masking/network-tools',
						'masking/scan',
					],
				},
				{
					label: 'Optional checks',
					items: [
						'checks',
						'checks/sensitivity',
						'checks/local',
						'checks/skills',
						'checks/handback',
						'checks/review',
					],
				},
				{
					label: 'Watch and audit',
					items: ['watch/extensions', 'watch/audit'],
				},
				{
					label: 'Reference',
					items: ['reference/configuration', 'reference/storage'],
				},
				{
					label: 'About',
					items: [
						'about/how-it-works',
						'about/accuracy',
						'about/limitations',
						'about/development',
						{ label: 'Security policy', link: `${repo}/blob/main/SECURITY.md`, attrs: { target: '_blank' } },
						{ label: 'Privacy', link: 'https://keepmum.app/privacy' },
					],
				},
			],
		}),
	],
});

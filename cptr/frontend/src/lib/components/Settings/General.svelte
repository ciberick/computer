<script lang="ts">
	import {
		appVersion,
		showChangelog,
		streamingBehavior,
		showUpdateToastPref,
		updateAvailable,
		latestVersion
	} from '$lib/stores';
	import type { StreamingBehavior } from '$lib/stores';
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { getAdminConfig, updateConfig } from '$lib/apis/admin';
	import { t, locale, changeLocale, supportedLocales } from '$lib/i18n';
	import { session } from '$lib/session';
	import ToggleSwitch from '../common/ToggleSwitch.svelte';

	interface Props {
		showPwaSettings?: boolean;
	}

	let { showPwaSettings = false }: Props = $props();

	const REPO_URL = 'https://github.com/open-webui/computer';
	const SHARE_TEXT = 'Check out Computer. Your computer, from anywhere.';

	const shareLinks = [
		{
			label: 'X',
			href: `https://x.com/intent/tweet?text=${encodeURIComponent(SHARE_TEXT)}&url=${encodeURIComponent(REPO_URL)}`
		},
		{
			label: 'Reddit',
			href: `https://reddit.com/submit?url=${encodeURIComponent(REPO_URL)}&title=${encodeURIComponent(SHARE_TEXT)}`
		},
		{
			label: 'LinkedIn',
			href: `https://www.linkedin.com/sharing/share-offsite/?url=${encodeURIComponent(REPO_URL)}`
		}
	];

	let copied = $state(false);
	let resetting = $state(false);

	// cptr tools exposed to external agents (Claude Code, Codex…) over MCP. Server config, admin only.
	const AGENT_TOOL_GROUPS = ['automations', 'notifications', 'memory'] as const;
	type AgentToolGroup = (typeof AGENT_TOOL_GROUPS)[number];

	let agentToolsLoaded = $state(false);
	let agentToolsEnabled = $state(false);
	let agentToolGroups = $state<Record<AgentToolGroup, boolean>>({
		automations: true,
		notifications: true,
		memory: true
	});
	let memoryEnabled = $state(true);

	onMount(async () => {
		if ($session?.role !== 'admin') return;
		try {
			const config = await getAdminConfig();
			agentToolsEnabled =
				config['agents.cptr_tools.enabled'] === true ||
				config['agents.cptr_tools.enabled'] === 'true';
			const groups = config['agents.cptr_tools.groups'];
			if (groups && typeof groups === 'object') {
				for (const group of AGENT_TOOL_GROUPS) {
					agentToolGroups[group] = (groups as Record<string, unknown>)[group] !== false;
				}
			}
			const memory = config['memory.enabled'];
			memoryEnabled = !(memory === false || memory === 'false' || memory === '0');
			agentToolsLoaded = true;
		} catch {
			toast.error($t('admin.failedToLoadConfig'));
		}
	});

	async function saveAgentTools(updates: Record<string, unknown>, revert: () => void) {
		try {
			await updateConfig(updates);
		} catch {
			revert();
			toast.error($t('admin.failedToSave'));
		}
	}

	function setAgentToolsEnabled(value: boolean) {
		const previous = agentToolsEnabled;
		agentToolsEnabled = value;
		saveAgentTools({ 'agents.cptr_tools.enabled': value }, () => (agentToolsEnabled = previous));
	}

	function setAgentToolGroup(group: AgentToolGroup, value: boolean) {
		const previous = { ...agentToolGroups };
		agentToolGroups = { ...agentToolGroups, [group]: value };
		saveAgentTools(
			{ 'agents.cptr_tools.groups': agentToolGroups },
			() => (agentToolGroups = previous)
		);
	}

	function copyLink() {
		navigator.clipboard.writeText(REPO_URL);
		copied = true;
		setTimeout(() => (copied = false), 2000);
	}

	async function resetPwa() {
		resetting = true;

		try {
			if ('serviceWorker' in navigator) {
				const registrations = await navigator.serviceWorker.getRegistrations();
				await Promise.all(registrations.map((registration) => registration.unregister()));
			}

			if ('caches' in window) {
				const keys = await caches.keys();
				await Promise.all(
					keys.filter((key) => key.startsWith('cptr-')).map((key) => caches.delete(key))
				);
			}
		} finally {
			location.reload();
		}
	}
</script>

<div class="flex flex-col h-full">
	<div class="flex-1 min-h-0 overflow-y-auto scrollbar-hover pr-1.5 -mr-1.5">
		<h2 class="text-sm font-medium text-gray-900 dark:text-white mb-4">{$t('general.title')}</h2>

		<div class="mb-5">
			<div class="flex items-baseline gap-2">
				<a
					href={REPO_URL}
					target="_blank"
					rel="noopener noreferrer"
					class="text-xs font-semibold text-gray-900 dark:text-white hover:underline">Computer</a
				>
				{#if $appVersion}
					<button
						onclick={() => showChangelog.set(true)}
						class="text-[0.6875rem] text-gray-400 dark:text-gray-600 hover:text-gray-500 dark:hover:text-gray-400 font-mono hover:underline cursor-pointer"
						>v{$appVersion}</button
					>
				{/if}
				{#if $updateAvailable}
					<span class="text-[0.6875rem] text-gray-300 dark:text-gray-600">·</span>
					<a
						href="https://github.com/open-webui/computer/releases"
						target="_blank"
						rel="noopener noreferrer"
						class="text-[0.6875rem] text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
						>{$t('about.updateAvailable', { version: $latestVersion })}</a
					>
				{/if}
			</div>

			<p class="text-[0.8125rem] text-gray-500 mt-0.5 mb-2">{$t('app.tagline')}</p>

			<div class="flex items-center gap-1.5">
				<span class="text-[0.6875rem] text-gray-400 dark:text-gray-600 mr-1"
					>{$t('about.share')}</span
				>
				{#each shareLinks as link, i}
					{#if i > 0}
						<span class="text-[0.6875rem] text-gray-200 dark:text-gray-700">·</span>
					{/if}
					<a
						href={link.href}
						target="_blank"
						rel="noopener noreferrer"
						class="text-[0.6875rem] text-gray-400 hover:text-gray-600 dark:hover:text-gray-500 transition-colors"
						>{link.label}</a
					>
				{/each}
				<span class="text-[0.6875rem] text-gray-200 dark:text-gray-700">·</span>
				<button
					onclick={copyLink}
					class="text-[0.6875rem] text-gray-400 hover:text-gray-600 dark:hover:text-gray-500 transition-colors cursor-pointer"
				>
					{copied ? $t('about.copied') : $t('about.copyLink')}
				</button>
			</div>
		</div>

		<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2">{$t('general.language')}</h3>
		<select
			class="w-full max-w-[12.5rem] bg-transparent text-[0.8125rem] text-gray-700 dark:text-gray-300 outline-none py-1 cursor-pointer"
			value={$locale}
			onchange={(e) => changeLocale((e.currentTarget as HTMLSelectElement).value)}
		>
			{#each supportedLocales as loc}
				<option value={loc.code}>{loc.label}</option>
			{/each}
		</select>

		{#if $session?.role === 'admin'}
			<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">{$t('general.updates')}</h3>
			<label class="flex items-center justify-between cursor-pointer">
				<span class="text-xs text-gray-600 dark:text-gray-400"
					>{$t('general.updateNotifications')}</span
				>
				<ToggleSwitch value={$showUpdateToastPref} onchange={(v) => showUpdateToastPref.set(v)} />
			</label>
			<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600 mt-1">
				{$t('general.updateNotificationsDesc')}
			</p>

			{#if agentToolsLoaded}
				<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">
					{$t('general.agentTools')}
				</h3>
				<label class="flex items-center justify-between cursor-pointer">
					<span class="text-xs text-gray-600 dark:text-gray-400"
						>{$t('general.agentToolsEnabled')}</span
					>
					<ToggleSwitch
						value={agentToolsEnabled}
						onchange={setAgentToolsEnabled}
						ariaLabel={$t('general.agentToolsEnabled')}
					/>
				</label>
				<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600 mt-1">
					{$t('general.agentToolsDesc')}
				</p>

				{#if agentToolsEnabled}
					<div class="flex flex-col gap-2 mt-2.5 pl-3">
						{#each AGENT_TOOL_GROUPS as group}
							{@const unavailable = group === 'memory' && !memoryEnabled}
							<label
								class="flex items-center justify-between
								{unavailable ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}"
							>
								<span class="text-xs text-gray-600 dark:text-gray-400"
									>{$t(`models.builtinTools.${group}`)}</span
								>
								<ToggleSwitch
									value={agentToolGroups[group] && !unavailable}
									disabled={unavailable}
									onchange={(v) => setAgentToolGroup(group, v)}
									ariaLabel={$t(`models.builtinTools.${group}`)}
								/>
							</label>
						{/each}
						{#if !memoryEnabled}
							<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
								{$t('general.agentToolsMemoryDisabled')}
							</p>
						{/if}
					</div>
				{/if}
			{/if}
		{/if}

		<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">{$t('general.messageQueue')}</h3>
		<div class="flex gap-1">
			{#each [{ value: 'queue' as StreamingBehavior, label: $t('general.queue') }, { value: 'interrupt' as StreamingBehavior, label: $t('general.interrupt') }] as opt}
				<button
					class="flex items-center gap-1.5 h-7 px-2.5 rounded-lg text-xs transition-colors duration-100
					{$streamingBehavior === opt.value
						? 'bg-gray-200/50 dark:bg-white/8 text-gray-900 dark:text-white font-medium'
						: 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'}"
					onclick={() => streamingBehavior.set(opt.value)}
				>
					{opt.label}
				</button>
			{/each}
		</div>
		<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600 mt-1">
			{$streamingBehavior === 'queue' ? $t('general.queueDesc') : $t('general.interruptDesc')}
		</p>

		<div class="pt-5">
			<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-1">
				{$t('about.license')}
			</h3>
			<p class="text-[0.6875rem] text-gray-500 leading-relaxed font-mono whitespace-pre-line">
				<a
					href="https://github.com/open-webui/computer/blob/main/LICENSE"
					target="_blank"
					rel="noopener noreferrer"
					class="underline hover:text-gray-700 dark:hover:text-gray-300"
					>{$t('about.licenseName')}</a
				>

				<br />
				{$t('about.copyright')}
			</p>
			<p class="text-[0.6875rem] text-gray-300 dark:text-gray-700 pt-4">
				{$t('about.createdBy')}
			</p>

			{#if showPwaSettings}
				<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">{$t('pwa.resetTitle')}</h3>
				<button
					class="text-[0.8125rem] text-gray-500 dark:text-gray-500 hover:text-gray-900 dark:hover:text-white transition-colors disabled:opacity-40 disabled:pointer-events-none"
					onclick={resetPwa}
					disabled={resetting}
				>
					{resetting ? $t('pwa.resetting') : $t('pwa.reset')}
				</button>
				<p class="text-[0.6875rem] text-gray-400 dark:text-gray-600 mt-1">
					{$t('pwa.resetDesc')}
				</p>
			{/if}
		</div>
	</div>
</div>

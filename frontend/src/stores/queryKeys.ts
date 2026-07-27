export const Q = {
  targets: (checkType?: string) => checkType ? `targets:${checkType}` : 'targets',
  target: (id: string | number) => `target:${id}`,
  targetChanges: (id: string | number) => `target:${id}:changes`,
  recentChanges: () => 'changes:recent',
  targetErrors: (id: string | number) => `target:${id}:errors`,
  targetSelectors: (id: string | number) => `target:${id}:selectors`,
  targetTriggers: (id: string | number) => `target:${id}:triggers`,

  workflows: () => 'workflows',
  workflow: (id: string | number) => `workflow:${id}`,
  workflowTasks: (id: string | number) => `workflow:${id}:tasks`,
  workflowData: (id: string | number, params?: string) =>
    params ? `workflow:${id}:data:${params}` : `workflow:${id}:data`,
  workflowDataFacets: (id: string | number, params?: string) =>
    params ? `workflow:${id}:data:facets:${params}` : `workflow:${id}:data:facets`,
  dataWorkflows: () => 'data:workflows',

  // Dragnet — distributed site crawls
  crawls: () => 'crawls',
  crawl: (id: string | number) => `crawl:${id}`,

  triggers: () => 'triggers',
  flowReferences: () => 'flowReferences',
  webhookTriggers: () => 'webhookTriggers',

  me: () => 'auth:me',
  socialAccounts: () => 'auth:social:accounts',
  socialProviders: () => 'auth:social:providers',
  mfaStatus: () => 'auth:mfa:status',

  tasks: (params?: string) => params ? `tasks:${params}` : 'tasks',
  recentTasks: () => 'tasks:recent',

  agents: () => 'agents',
  agentMonitoring: () => 'agents:monitoring',
  userAgents: () => 'user-agents',
  recorderCapability: () => 'recorder-capability',

  apiKeys: () => 'apiKeys',
  team: () => 'team',
  credits: () => 'credits',
  creditLedger: () => 'credits:ledger',
  creditStats: () => 'credits:stats',
  creditPacks: () => 'credits:packs',

  integrations: () => 'integrations',
  oauthApps: () => 'oauth:apps',
  oauthConnections: () => 'oauth:connections',

  managedEndpoints: () => 'managedEndpoints',
  consumerKeys: () => 'consumerKeys',

  personas: (domain?: string) => domain ? `personas:${domain}` : 'personas',
  mailConnections: () => 'mailConnections',

  executions: (triggerId?: string | number) => triggerId ? `executions:${triggerId}` : 'executions',

  // admin
  adminStats: () => 'admin:stats',
  adminUsers: (params?: string) => params ? `admin:users:${params}` : 'admin:users',
  adminOrgs: (params?: string) => params ? `admin:orgs:${params}` : 'admin:orgs',
  adminWorkflows: (params?: string) => params ? `admin:workflows:${params}` : 'admin:workflows',
  adminTargets: (params?: string) => params ? `admin:targets:${params}` : 'admin:targets',
  adminCapacity: () => 'admin:capacity',
  adminMarketplaceReports: (params?: string) => params ? `admin:mktReports:${params}` : 'admin:mktReports',
  adminVirtualAgents: () => 'admin:virtualAgents',
  adminServerlessAgents: () => 'admin:serverlessAgents',
  adminCloudProviders: (params?: string) => params ? `admin:cloudProviders:${params}` : 'admin:cloudProviders',
  adminSettings: () => 'admin:settings',
  adminPlans: () => 'admin:plans',
  adminNodes: () => 'admin:nodes',
  adminFleet: () => 'admin:fleet',
  adminLogs: (params?: string) => params ? `admin:logs:${params}` : 'admin:logs',
  adminOrgEconomics: (orgId: string) => `admin:org:${orgId}:economics`,
  adminOrgPaymentMethods: (orgId: string) => `admin:org:${orgId}:payment-methods`,
  adminOrgInvoices: (orgId: string) => `admin:org:${orgId}:invoices`,
  adminAbuseSignals: (params?: string) => params ? `admin:abuse:signals:${params}` : 'admin:abuse:signals',
  adminAbuseBlocklist: () => 'admin:abuse:blocklist',
  adminAbuseSupply: () => 'admin:abuse:supply',
  adminAbuseRelays: () => 'admin:abuse:relays',
  adminAbuseAlertConfig: () => 'admin:abuse:alert-config',

  // Streaming
  streamingSessions: (status?: string) => status ? `streaming:sessions:${status}` : 'streaming:sessions',
  streamingSession: (key: string) => `streaming:session:${key}`,

  // Marketplace
  marketplaceListings: (params?: string) => params ? `marketplace:listings:${params}` : 'marketplace:listings',
  marketplaceListing: (slug: string) => `marketplace:listing:${slug}`,
  marketplaceCapability: (key: string) => `marketplace:capability:${key}`,
  marketplaceMyListings: () => 'marketplace:my-listings',
  marketplaceInstalls: () => 'marketplace:installs',
  marketplaceInstallManifest: (slug: string) => `marketplace:install:${slug}:manifest`,
  marketplaceReviews: (slug: string) => `marketplace:listing:${slug}:reviews`,
  marketplaceReviewEligibility: (slug: string) => `marketplace:listing:${slug}:review-eligibility`,
  invitationsReceived: () => 'invitations:received',

  // Vault secrets (marketplace attach picker)
  vaultSecrets: () => 'vault:secrets',

  // Generic key helper
  key: (k: string) => k,
} as const;

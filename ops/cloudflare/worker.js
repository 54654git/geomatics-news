// Optional independent trigger. Deploy only in the owner's Cloudflare account.
export default {
  async scheduled(controller, env, ctx) {
    const response = await fetch(
      `https://api.github.com/repos/${env.OWNER}/${env.REPO}/actions/workflows/${env.WORKFLOW}/dispatches`,
      {method: 'POST', headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: 'application/vnd.github+json',
        'Content-Type': 'application/json', 'User-Agent': 'geomatics-weekly-trigger',
        'X-GitHub-Api-Version': '2022-11-28'
      }, body: JSON.stringify({ref: env.REF, inputs: {force: false}})}
    );
    // 204 confirms dispatch acceptance, not successful generation. The workflow
    // checks the weekly period, handles duplicates and exposes coverage failures.
    if (response.status !== 204) throw new Error(`GitHub dispatch rejected: HTTP ${response.status}`);
    console.log('Dispatch accepted; check GitHub Actions and reports/health.json for delivery.');
  }
};

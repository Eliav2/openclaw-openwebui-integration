// Fold Responses stream events the way an Open WebUI chat tab does while a
// reply streams (the vendored `applyResponseStreamEvent`), for the live-order
// tests (ELI-122), and Open WebUI's `chat:tasks:cancel`. Reads a JSON array
// of events on stdin, prints the output list the tab renders.
//   bun scripts/owui_live_fold.ts < events.json
import { applyResponseStreamEvent } from '../tests/fixtures/test_fixture_owui_structured_output.ts';

const events = JSON.parse(await Bun.stdin.text());
let output = [];
for (const event of events) {
	// Open WebUI's cancel of a reply replaces the message's output with its
	// own copy (Chat.svelte, the `chat:tasks:cancel` handler).
	output =
		event.type === 'chat:tasks:cancel'
			? event.data.output
			: applyResponseStreamEvent(output, event);
}
process.stdout.write(JSON.stringify(output));

"""Devin client tests for request shape, error handling, pagination, and bootstrap reuse.

ELI5: every test uses an in-process HTTP transport, so it checks the provider
contract without sending a real request or exposing a credential.
"""

import json  # Decode and inspect request bodies sent to the mock transport.
import sys  # Locate the interpreter running these provider-boundary tests.
from pathlib import Path  # Point the mock settings at the repository checkout.

import httpx  # Supply deterministic HTTP responses without network access.
import pytest  # Parameterize provider status cases and expected failures.

import app.devin as devin_module
from app.cases import Registry  # Load the trusted case prompt and evidence contract.
from app.devin import Devin, PLAYBOOK, RemoteError, context_resource_name  # Exercise the provider client and sanitized errors.
from app.models import Job  # Build a minimal job for session creation.


def adapter(settings, handler):
    """Build a Devin client backed by a caller-supplied in-process HTTP handler."""

    client = httpx.Client(base_url=f"https://api.devin.ai/v3/organizations/{settings.devin_org_id}/",  # Scope URLs to one organization.
                          transport=httpx.MockTransport(handler))  # Prevent all network traffic.
    return Devin(settings, client)  # Use production request and response handling.


def enable_local_validator(settings):
    """Point a provider mock at the running test interpreter so launch gates can pass."""
    # ELI5: the mock launch still checks that a real local evaluator exists before posting.
    settings.superset_repo_path = Path.cwd()
    settings.superset_python = Path(sys.executable)


def test_session_creation_uses_v3_context_evidence_and_budget(live):
    """Send the v3 context, evidence, tags, and configured ACU budget on launch."""

    enable_local_validator(live)
    calls = []  # Record request order for the attachment and session calls.

    def handler(req):
        """Validate each mock request and return the expected provider response."""

        calls.append(req)  # Preserve the request for the final order assertion.
        assert req.headers["Authorization"] == "Bearer simulation-only"
        if req.url.path.endswith("/attachments"):
            assert "multipart/form-data" in req.headers["Content-Type"]  # Evidence is uploaded as a form attachment.
            assert b"baseline_proof" in req.content  # The attachment contains the proof marker.
            return httpx.Response(200, json={"url": "https://attachments.example/evidence"})  # Return the attachment URL.
        body = json.loads(req.content)  # Decode the session request body.
        assert body["repos"] == [live.github_repository]  # Limit Devin to the configured repository.
        assert body["max_acu_limit"] == live.devin_max_acu  # Forward the per-session budget.
        assert body["playbook_id"] == "playbook-test"  # Reuse the bootstrapped playbook.
        assert body["knowledge_ids"] == ["note-test"]  # Include the bootstrapped knowledge note.
        assert body["attachment_urls"] == ["https://attachments.example/evidence"]  # Link the uploaded proof.
        assert "drp-job-test" in body["tags"]  # Tag the session for reconciliation.
        assert body["structured_output_required"]  # Require the structured provider response.
        assert "preserve" in body["prompt"].lower()  # Ensure the safety prompt was included.
        return httpx.Response(200, json={"session_id": "devin-test", "status": "new", "url": "https://app.devin.ai/s/test"})  # Return a valid session.

    devin = adapter(live, handler)  # Build the client under test.
    case = Registry(live).cases["histogram-invalid-column"]  # Select one approved case.
    job = Job(id="job-test", repository=live.github_repository, issue_number=101)  # Build the minimal job payload.
    assert devin.create_session(job, case).session_id == "devin-test"  # Confirm the session ID is parsed.
    assert [req.url.path for req in calls] == [  # Attachment must precede the session request.
        "/v3/organizations/org-test/attachments", "/v3/organizations/org-test/sessions"]


def test_session_creation_rejects_missing_validator_before_provider_call(live, tmp_path):
    """A missing Superset interpreter is rejected before any attachment or session request."""
    # ELI5: point the local evaluator at a path that cannot execute candidate code.
    live.superset_python = tmp_path / "missing-python"
    calls = []

    def handler(request):
        """Record an unexpected provider call so the assertion can prove none occurred."""
        calls.append(request)
        return httpx.Response(500)

    devin = adapter(live, handler)
    case = Registry(live).cases["histogram-invalid-column"]
    job = Job(id="job-test", repository=live.github_repository, issue_number=101)
    with pytest.raises(ValueError, match="validation environment"):
        devin.create_session(job, case)
    assert calls == []


def test_session_creation_rejects_incomplete_context_before_attachment(live):
    """A bootstrap context without both reusable resource IDs cannot reach the provider."""
    enable_local_validator(live)
    # ELI5: remove one required ID while retaining valid JSON and repository identity.
    context_path = live.storage / "context.json"
    context = json.loads(context_path.read_text())
    context.pop("note_id")
    context_path.write_text(json.dumps(context))
    calls = []

    def handler(request):
        """Record an unexpected attachment request for the no-call assertion."""
        calls.append(request)
        return httpx.Response(500)

    devin = adapter(live, handler)
    case = Registry(live).cases["histogram-invalid-column"]
    job = Job(id="job-test", repository=live.github_repository, issue_number=101)
    with pytest.raises(ValueError, match="Bootstrap context is incomplete"):
        devin.create_session(job, case)
    assert calls == []


def test_application_attachment_omits_review_only_fix_reference(live):
    """The Devin attachment omits the inspection note while retaining local case identity."""
    enable_local_validator(live)
    # ELI5: record the branch required by the application case without changing its frozen fields.
    context_path = live.storage / "context.json"
    context = json.loads(context_path.read_text())
    context["base_branches"] = {"import-unparseable-yaml": "remediation-import-yaml"}
    context_path.write_text(json.dumps(context))
    registry = Registry(live)
    case = registry.cases["import-unparseable-yaml"]
    fingerprint = case.fingerprint
    calls = []

    def handler(request):
        """Assert review-only provenance is absent from the uploaded agent context."""
        calls.append(request)
        if request.url.path.endswith("/attachments"):
            # ELI5: the upload may include the contract, but never the inspection/fix-reference note.
            assert b"inspection" not in request.content
            assert b"22ec1f598808859c42dccff21665786224127122" not in request.content
            return httpx.Response(200, json={"url": "https://attachments.example/evidence"})
        return httpx.Response(200, json={"session_id": "devin-app", "status": "new", "url": "https://app.devin.ai/s/app"})

    devin = adapter(live, handler)
    job = Job(id="job-app", repository=live.github_repository, issue_number=105)
    assert devin.create_session(job, case).session_id == "devin-app"
    assert case.fingerprint == fingerprint
    assert registry.evidence(case)["outcome"] == "CONFIRMED"
    assert len(calls) == 2


def test_get_session_and_correction(live):
    """Parse a session response and send a scoped correction message."""

    calls = []  # Record method and path for the mock requests.

    def handler(req):
        """Return a session for GET and accept the exact correction for POST."""

        calls.append((req.method, req.url.path))  # Preserve the request sequence.
        if req.method == "POST":
            assert json.loads(req.content) == {"message": "Scoped correction"}  # Send only the requested correction text.
            return httpx.Response(204)  # Accept the correction without a response body.
        return httpx.Response(200, json={"session_id": "devin-test", "status": "suspended", "status_detail": "finished"})  # Return the suspended session.

    devin = adapter(live, handler)  # Build the client with the mock transport.
    assert devin.get_session("devin-test").status == "suspended"  # Parse the provider state.
    devin.send_correction("devin-test", "Scoped correction")  # Send the bounded correction.
    assert calls[-1] == ("POST", "/v3/organizations/org-test/sessions/devin-test/messages")  # Use the message endpoint.


@pytest.mark.parametrize("code,retryable", [(401, False), (403, False), (429, True), (500, True), (503, True)])
def test_remote_errors_are_sanitized(live, code, retryable):
    """Classify retryable provider statuses and remove response bodies from errors."""

    devin = adapter(live, lambda req: httpx.Response(code, text="SECRET private prompt", headers={"Retry-After": "1000"}))  # Return a status with a secret-looking body.
    with pytest.raises(RemoteError) as error:  # The client must convert HTTP failure to its safe error type.
        devin.get_session("test")  # Trigger one request.
    assert error.value.retryable is retryable  # Preserve the status-specific retry policy.
    assert error.value.retry_after == 60  # Clamp an excessive server delay to one minute.
    assert "SECRET" not in str(error.value)  # Do not leak provider response text.


def test_timeout_is_not_automatically_retried(live):
    """Surface a network timeout after one attempt instead of retrying automatically."""

    calls = []  # Count requests made before the timeout is surfaced.

    def handler(req):
        """Raise a transport timeout while recording the attempted request."""

        calls.append(req)  # Record the one attempted request.
        raise httpx.ReadTimeout("secret", request=req)  # Simulate a network timeout without a retry.

    with pytest.raises(RemoteError, match="timeout"):  # The client exposes a safe timeout error.
        adapter(live, handler).get_session("test")  # Trigger the request.
    assert len(calls) == 1  # Confirm no automatic second attempt occurred.


@pytest.mark.parametrize("data", [{}, {"session_id": "bad/path", "status": "new"},
    {"session_id": "test", "status": "new", "url": "javascript:alert(1)"}])
def test_invalid_response_is_rejected(live, data):
    """Reject missing or unsafe fields in an otherwise successful session response."""

    with pytest.raises(RemoteError, match="Invalid session"):  # Invalid provider data fails closed.
        adapter(live, lambda req: httpx.Response(200, json=data)).get_session("test")  # Feed the malformed response.


def test_invalid_json(live):
    """Reject a successful HTTP response whose body is not JSON."""

    with pytest.raises(RemoteError, match="JSON"):  # Parsing failure is a provider error.
        adapter(live, lambda req: httpx.Response(200, text="not-json")).get_session("test")  # Return invalid JSON.


def test_session_reconciliation_follows_cursors(live):
    """Follow a provider pagination cursor until the tagged session is found."""

    def handler(req):
        """Return an empty first page and the tagged session on the cursor page."""

        if "after" not in req.url.params:
            return httpx.Response(200, json={"items": [], "has_next_page": True, "end_cursor": "cursor-2"})  # Ask the client to continue.
        assert req.url.params["after"] == "cursor-2"  # Verify the returned cursor is reused.
        return httpx.Response(200, json={"items": [{"session_id": "found", "status": "running", "tags": ["drp-job"]}],  # Return the matching session.
                                         "has_next_page": False})  # End pagination.

    assert adapter(live, handler).find_session("job").session_id == "found"  # Reconciliation returns the tagged session.


def test_duplicate_tag_and_broken_cursor_fail_closed(live):
    """Reject ambiguous tags and a cursor that repeats forever."""

    record = {"session_id": "found", "status": "running", "tags": ["drp-job"]}  # Model one matching session.
    devin = adapter(live, lambda req: httpx.Response(200, json={"items": [record, record]}))  # Return the same match twice.
    with pytest.raises(RemoteError, match="Multiple sessions"):  # Ambiguity must fail closed.
        devin.find_session("job")  # Search for the duplicated tag.
    devin = adapter(live, lambda req: httpx.Response(200, json={"items": [], "has_next_page": True, "end_cursor": "same"}))  # Return a stuck cursor.
    with pytest.raises(RemoteError, match="cursor"):  # A repeated cursor must stop pagination.
        devin.find_session("job")  # Trigger the broken-cursor guard.


def test_context_bootstrap_is_reusable_and_uses_notes_endpoint(live):
    """Create the context bundle once and reuse it on a second bootstrap call."""

    resources = {"playbooks": [], "knowledge/notes": []}  # Hold provider resources in memory.
    writes = []  # Record resource creation paths.

    def handler(req):
        """List existing resources or append a newly created resource."""

        path = req.url.path.split("org-test/")[1]  # Remove the organization prefix.
        if req.method == "GET":
            return httpx.Response(200, json={"items": resources[path], "has_next_page": False})  # Return all existing resources.
        body = json.loads(req.content)  # Decode the resource creation request.
        key = "playbook_id" if path == "playbooks" else "note_id"  # Select the provider ID field.
        resource_number = len(resources[path]) + 1  # Give each newly named resource a distinct deterministic test ID.
        resources[path].append({**body, key: f"new-{key}-{resource_number}"})  # Store the created resource.
        writes.append(path)  # Record that this endpoint wrote once.
        return httpx.Response(200, json=resources[path][-1])  # Return the created resource.

    devin = adapter(live, handler)  # Build the client with reusable in-memory resources.
    first = devin.bootstrap(Registry(live))  # Create the playbook and note.
    assert first == devin.bootstrap(Registry(live))  # The second call should reuse the same IDs.
    assert writes == ["playbooks", "knowledge/notes"]  # Each resource endpoint is written exactly once.
    assert resources["playbooks"][0]["body"] == PLAYBOOK  # Persist the expected safe playbook body.
    assert "build_sqlalchemy_uri" not in resources["playbooks"][0]["body"]  # Do not include an unrelated prompt.
    assert resources["playbooks"][0]["title"] == context_resource_name(live.github_repository, PLAYBOOK)
    assert resources["playbooks"][0]["title"].endswith("-v2")
    assert live.github_repository in resources["knowledge/notes"][0]["trigger"]
    assert "application" in resources["knowledge/notes"][0]["trigger"]
    assert "test-quality" in resources["knowledge/notes"][0]["trigger"]


def test_context_body_change_gets_new_name_without_overwriting_old_resource(live, monkeypatch):
    """A changed playbook body creates a new content-addressed resource and preserves the old one."""
    resources = {"playbooks": [], "knowledge/notes": []}
    writes = []

    def handler(req):
        """Serve the in-memory resource catalog and record every creation request."""
        path = req.url.path.split("org-test/")[1]
        if req.method == "GET":
            return httpx.Response(200, json={"items": resources[path], "has_next_page": False})
        body = json.loads(req.content)
        key = "playbook_id" if path == "playbooks" else "note_id"
        number = len(resources[path]) + 1
        resources[path].append({**body, key: f"new-{key}-{number}"})
        writes.append(path)
        return httpx.Response(200, json=resources[path][-1])

    devin = adapter(live, handler)
    first = devin.bootstrap(Registry(live))
    original = resources["playbooks"][0].copy()
    monkeypatch.setattr(devin_module, "PLAYBOOK", PLAYBOOK + "\nA bounded v2 wording change.")
    second = devin.bootstrap(Registry(live))
    assert writes == ["playbooks", "knowledge/notes", "playbooks"]
    assert len(resources["playbooks"]) == 2
    assert resources["playbooks"][0] == original
    assert resources["playbooks"][1]["title"] != original["title"]
    assert second["playbook_id"] != first["playbook_id"]
    assert second["note_id"] == first["note_id"]


def test_context_resource_name_keeps_repositories_distinct():
    """Different repository spellings must not collapse to one reusable Playbook name."""

    # ELI5: underscores and hyphens produce the same readable slug, so the raw hash must differ.
    first = context_resource_name("owner/re_po", PLAYBOOK)
    second = context_resource_name("owner/re-po", PLAYBOOK)
    assert first != second


def test_context_rejects_drift(live):
    """Reject a provider context resource whose body drifts from the trusted contract."""

    mismatched_name = context_resource_name(live.github_repository, PLAYBOOK)
    devin = adapter(live, lambda req: httpx.Response(200, json={"items": [  # Return one mismatched existing playbook.
        {"title": mismatched_name, "body": "unexpected", "playbook_id": "id"}]}))
    with pytest.raises(ValueError, match="differs"):  # Context drift must stop bootstrap.
        devin.bootstrap(Registry(live))  # Compare the provider resource with the trusted body.


def test_http_client_is_org_scoped_and_credential_not_in_url(live):
    """Keep the client scoped to one organization and keep the credential in headers."""

    devin = Devin(live)  # Build the real client with fixture settings.
    assert str(devin.client.base_url) == "https://api.devin.ai/v3/organizations/org-test/"  # Scope every endpoint to the organization.
    assert devin.client.headers["Authorization"].startswith("Bearer ")  # Send the credential only as an auth header.
    assert devin.client.follow_redirects is False  # Do not let redirects move credentials to another host.
    devin.client.close()  # Release the HTTP client's resources.

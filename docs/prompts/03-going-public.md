# Prompt — going public

Not an implementation prompt. Run this in the `quotalens` repo when the tag is
close, to get the plan for turning a working tool into a project strangers can
use, trust and contribute to. The output is documents and a checklist, not code.

---

QuotaLens is about to go public: MIT, `github.com/amitshcc/quotalens`, PyPI,
and a post announcing it. Read `README.md`, `VISION.md`, `docs/MVP-SCOPE.md`,
`docs/FEATURE-REVIEW.md` and `docs/RELEASE-CHECKLIST.md` first — the strategy is
mostly already decided in those and I want you to find where it isn't, rather
than restate it.

Push back on me. If something below is a bad idea, say so.

## 1. What has to be true before the repo is public

Give me a checklist I can actually run, not a list of best practices. At
minimum, work out the answers to these and tell me which ones are not yet true:

- **History.** Has any cookie, org id, real database, log or personal path ever
  been committed? Check the whole history, not the tip. If something is in
  there, the fix is a rewrite before the first push, not a follow-up commit.
- **The credential story, written for a skeptic.** A stranger is being asked to
  paste a session cookie into a tool they found on the internet. There should be
  one document that says exactly what is stored where, what is sent where, and
  how they can verify both claims themselves in five minutes. That document is
  the single highest-leverage thing in this repo for adoption, and it does not
  exist yet.
- **The name.** "QuotaLens" and `quotalens.com` — check PyPI availability, npm
  squatting, and whether anything with that name already exists in this space.
  Also: is there any trademark risk in the way the site and README refer to
  Claude and Anthropic? We claim no association; make sure the wording actually
  supports that.
- **License and attribution.** MIT is decided. Confirm every borrowed idea,
  payload shape or reference implementation that informed this code is credited
  where it should be — ClaudeUsageBar and ccusage at minimum.
- **Security reporting.** A `SECURITY.md` with an address, because the first
  serious bug report on a credential-handling tool should not arrive as a public
  issue.

## 2. What has to be true before it can accept contributions

I want this to be maintainable by me, in evenings, without it becoming a second
job. Design for that constraint honestly rather than for an imagined community.

- `CONTRIBUTING.md` that sets expectations I can keep, including what I will not
  merge — new runtime dependencies, non-loopback binding, other providers,
  anything that spends quota.
- Issue templates beyond the redaction one: endpoint drift is the failure mode
  this project will actually see, so make the bug template collect exactly what
  is needed to diagnose drift, pre-redacted.
- A short document on how the parser is meant to survive Anthropic changing the
  payload, so a contributor can fix drift without me. That is the single
  maintenance risk that will decide whether this project lives.
- Versioning and release: what goes in a patch, what forces a minor, how a
  release is cut, and whether it should be automated now or later.

## 3. The roadmap after 1.0

`MVP-SCOPE.md` has a "what 1.0 is for" list and one genuinely open strategic
question: every serious competitor is going multi-provider, it violates no
stated non-goal, and it multiplies endpoint drift — our largest maintenance
risk — by the number of vendors. Do not answer it by drift.

Give me a recommendation with the reasoning, and sequence the post-1.0 list
against it: the local-session correlation overlay, reading Claude Code's
credentials as a second auth path, Docker with a file-based credential store,
the Grafana dashboard. For each: what problem it solves for whom, what it costs
in maintenance rather than in lines, and what evidence should trigger building
it. I would rather ship three things people asked for than eight I imagined.

## 4. The announcement

A LinkedIn post from me, plus a shorter variant I can adapt elsewhere.

- My voice, not launch copy. No emoji rows, no "excited to share", no thread of
  one-line paragraphs, no rhetorical question as the opener.
- The honest claim is the interesting one: Claude tells you what is consuming
  quota right now and then forgets; this remembers. That is a smaller pitch than
  most launches make, which is exactly why it will land with the people who have
  the problem.
- Say what it does not do and link the better tool for those cases. On LinkedIn
  that reads as confidence, and it is also true.
- Be accurate about the Terms position. Do not claim or imply endorsement,
  approval, or any association with Anthropic.
- One line on why it exists — I built it because I wanted the instrument and
  nothing kept the history.
- Give me two versions of different lengths and tell me which you would post.

## What I want back

Four things, as files in `docs/`: the pre-public checklist with each item marked
true or not-yet, the credential document written for the skeptic, the post-1.0
recommendation with the multi-provider call made, and the announcement drafts.
Then tell me the one thing on that list you think I will skip and shouldn't.

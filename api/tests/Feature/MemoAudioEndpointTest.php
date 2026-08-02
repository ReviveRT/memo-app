<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Contracts\AudioStorage;
use App\Repositories\MemoRepository;
use App\Services\Memos\Memo;
use App\Storage\LocalAudioStorage;
use Illuminate\Testing\TestResponse;
use Tests\Support\FakeMemoRepository;
use Tests\Support\RecordingAudioStorage;
use Tests\TestCase;

/**
 * MEMO-23: GET /api/memos/{id}/audio, and the byte ranges that make it playable.
 *
 * **The real LocalAudioStorage against a temp directory, not RecordingAudioStorage.** Every
 * other feature test here fakes the storage away, and each has the same reason: what it is
 * asserting is an ordering or a key, and both are properties of MemoService rather than of any
 * driver. This one is the opposite. A range response *is* an fseek and a bounded read over a
 * real file, so a double that returned bytes from an array would be asserting that the test
 * double can slice a string. The row is still faked -- the query behind it is one SELECT of two
 * columns and MEMO-25 owns running it against Postgres -- so what is real here is exactly the
 * half under test.
 *
 * The body is read by calling sendContent() into an output buffer, because BinaryFileResponse
 * holds a file rather than a string: `$response->getContent()` is `false` for one of these, and
 * the bytes only exist once it is asked to send them. That is also what makes the assertions
 * below honest about the range -- `offset` and `maxlen` are applied there and nowhere else, so
 * a Content-Range header that disagreed with the bytes would be caught.
 *
 * What this file cannot reach is the acceptance criterion itself: whether Safari plays and
 * seeks. That needs a browser and a pair of ears, and the task says so. What it can do is pin
 * every property Safari is known to need -- `Accept-Ranges`, a 206 with a correct
 * `Content-Range` for a real sub-range, and a 206 rather than a 200 for the `bytes=0-` a media
 * element opens with.
 */
final class MemoAudioEndpointTest extends TestCase
{
    private const MEMO_ID = '01984f2b-4c1a-7c3d-9f10-2b6c8d4e5a70';

    /**
     * 4 KiB of non-repeating bytes.
     *
     * Non-repeating matters: a range assertion over a run of identical bytes passes whatever
     * offset the response actually seeked to, which is the one thing these tests exist to
     * check. Every byte here is a function of its own position.
     */
    private const AUDIO_BYTES = 4_096;

    private FakeMemoRepository $repository;

    private string $root;

    private string $key;

    private string $contents;

    protected function setUp(): void
    {
        parent::setUp();

        $this->repository = new FakeMemoRepository;
        $this->app->instance(MemoRepository::class, $this->repository);

        $this->root = sys_get_temp_dir().'/memo-playback-'.bin2hex(random_bytes(6));
        mkdir($this->root, 0775);

        $this->app->instance(AudioStorage::class, new LocalAudioStorage($this->root));

        // A key shaped the way MemoService::createFromAudio writes them, so the filename in the
        // Content-Disposition below is the one a real recording would carry.
        $this->key = self::MEMO_ID.'.webm';
        $this->contents = $this->bytes(self::AUDIO_BYTES);

        file_put_contents("{$this->root}/{$this->key}", $this->contents);

        $this->repository->rows = [$this->voiceMemo()];
        $this->repository->audioPaths[self::MEMO_ID] = $this->key;
        $this->repository->audioMimes[self::MEMO_ID] = 'video/webm';
    }

    protected function tearDown(): void
    {
        foreach ((array) glob("{$this->root}/*") as $file) {
            @unlink((string) $file);
        }

        @rmdir($this->root);

        parent::tearDown();
    }

    public function test_the_whole_recording_comes_back_under_the_type_it_was_stored_as(): void
    {
        $response = $this->get($this->url());

        $response->assertOk()
            // The sniffed type from upload, not a guess made here. SniffedAudioType is what
            // wrote this value and what vouched for the file being a recording at all -- a
            // Chrome WebM sniffs as video/webm, which is the surprise that rule documents.
            ->assertHeader('Content-Type', 'video/webm')
            ->assertHeader('Content-Length', (string) self::AUDIO_BYTES)

            // The advertisement, and the half of Range support that is not a response to a
            // Range header: a player asks whether it may seek before it tries.
            ->assertHeader('Accept-Ranges', 'bytes')

            // Serving user-supplied bytes from this origin. The allowlist is the real
            // defence; this is what stops a browser looking for a better answer than it.
            ->assertHeader('X-Content-Type-Options', 'nosniff');

        $this->assertSame($this->contents, $this->body($response));
    }

    public function test_a_range_request_answers_206_with_exactly_those_bytes(): void
    {
        // The acceptance criterion, in the words the task uses: curl with a Range header
        // returns 206. A sub-range in the middle of the file, so neither end of it could be
        // produced by an off-by-one that happened to start at zero.
        $response = $this->get($this->url(), ['Range' => 'bytes=1000-1099']);

        $response->assertStatus(206)
            ->assertHeader('Content-Range', 'bytes 1000-1099/'.self::AUDIO_BYTES)
            ->assertHeader('Content-Length', '100')
            ->assertHeader('Content-Type', 'video/webm');

        $this->assertSame(substr($this->contents, 1000, 100), $this->body($response));
    }

    public function test_an_open_ended_range_from_zero_is_a_206_and_not_a_whole_file_200(): void
    {
        // **The Safari case, and the reason this test is separate from the one above.** A media
        // element opens with `Range: bytes=0-`, which asks for the whole file -- and a server
        // is free to answer that with a plain 200. Some do. Safari treats a 200 to a ranged
        // request as "this endpoint cannot seek" and refuses to play, so the thing worth
        // pinning is not that ranges work in general but that this one is a 206 too.
        $response = $this->get($this->url(), ['Range' => 'bytes=0-']);

        $response->assertStatus(206)
            ->assertHeader('Content-Range', 'bytes 0-'.(self::AUDIO_BYTES - 1).'/'.self::AUDIO_BYTES)
            ->assertHeader('Content-Length', (string) self::AUDIO_BYTES);

        $this->assertSame($this->contents, $this->body($response));
    }

    public function test_a_suffix_range_answers_with_the_end_of_the_file(): void
    {
        // `bytes=-256` means the last 256 bytes, not "from 0 to 256". It is how a player finds
        // the trailing metadata of a container, and it is the range form that is easy to parse
        // backwards -- which is one of the arguments for not parsing them here at all.
        $response = $this->get($this->url(), ['Range' => 'bytes=-256']);

        $response->assertStatus(206)
            ->assertHeader(
                'Content-Range',
                'bytes '.(self::AUDIO_BYTES - 256).'-'.(self::AUDIO_BYTES - 1).'/'.self::AUDIO_BYTES,
            );

        $this->assertSame(substr($this->contents, -256), $this->body($response));
    }

    public function test_a_range_past_the_end_of_the_file_is_refused_with_416(): void
    {
        $response = $this->get($this->url(), ['Range' => 'bytes=99999-100000']);

        // 416 with the size in it, which is what tells a client that guessed too far what the
        // file actually is. Answering 200 with the whole file instead -- the other plausible
        // reading of an impossible range -- would hand a player megabytes it did not ask for.
        $response->assertStatus(416)
            ->assertHeader('Content-Range', 'bytes */'.self::AUDIO_BYTES);
    }

    public function test_head_reports_the_length_and_range_support_without_a_body(): void
    {
        // What a player asks first. The length is what it sizes its scrubber from, and
        // Accept-Ranges is what tells it the scrubber will work.
        $response = $this->head($this->url());

        $response->assertOk()
            ->assertHeader('Accept-Ranges', 'bytes')
            ->assertHeader('Content-Length', (string) self::AUDIO_BYTES);

        $this->assertSame('', $this->body($response));
    }

    public function test_a_recording_may_be_cached_for_as_long_as_the_memo_lives(): void
    {
        $response = $this->get($this->url());

        // The opposite of the memo list's `no-store`, and deliberately: nothing rewrites
        // `audio_path`, so these bytes cannot change under this URL. Without this a scrub
        // re-fetches ranges the browser is already holding.
        $cacheControl = (string) $response->headers->get('Cache-Control');

        $this->assertStringContainsString('private', $cacheControl);
        $this->assertStringContainsString('immutable', $cacheControl);
        $this->assertStringContainsString('max-age=31536000', $cacheControl);

        // Played where it is asked to be played rather than offered as a download.
        $this->assertStringStartsWith(
            'inline;',
            (string) $response->headers->get('Content-Disposition'),
        );
    }

    public function test_a_typed_memo_has_nothing_to_play(): void
    {
        $this->repository->rows = [$this->textMemo()];
        unset($this->repository->audioPaths[self::MEMO_ID]);

        // A 404 rather than a 204 or an empty 200: there is no such resource, and a memo that
        // was typed never had one. The frontend does not ask for this URL at all on a text
        // memo -- see MemoDialog -- so reaching it means a hand-written request or a stale tab.
        $this->get($this->url())
            ->assertNotFound()
            ->assertJsonPath('message', 'That memo has no recording to play.');
    }

    public function test_a_memo_that_does_not_exist_is_a_404(): void
    {
        $this->repository->rows = [];

        $this->get($this->url())->assertNotFound();
    }

    public function test_a_row_whose_blob_is_gone_says_so_rather_than_saying_the_memo_has_none(): void
    {
        // A storage with no file behind the key, which is what `docker compose down -v` leaves
        // behind -- the database survives on its own volume and the recordings do not. The
        // sentence is different from the typed-memo 404 above on purpose: this one describes a
        // stack that has lost data and is worth investigating, and that one does not.
        $this->app->instance(AudioStorage::class, new RecordingAudioStorage);

        $this->get($this->url())
            ->assertNotFound()
            ->assertJsonPath('message', 'The recording for that memo is no longer on the audio volume.');
    }

    public function test_a_stored_type_this_app_would_not_accept_is_served_as_octet_stream(): void
    {
        // Unreachable through the upload route -- SniffedAudioType refuses everything not on
        // its list, and it is what writes this column -- so this is about a row from an older
        // build, a restored dump, or a hand-written INSERT. The one outcome that must not
        // happen is this origin serving `text/html` from a file somebody uploaded.
        $this->repository->audioMimes[self::MEMO_ID] = 'text/html';

        $this->get($this->url())
            ->assertOk()
            ->assertHeader('Content-Type', 'application/octet-stream');
    }

    public function test_an_id_that_is_not_a_uuid_matches_no_route(): void
    {
        // whereUuid on the route. Without it this reaches Postgres as `WHERE id = 'nonsense'`
        // and comes back as a 500 from `invalid input syntax for type uuid` -- which is the
        // reason every id in routes/api.php is constrained, stated there once for all of them.
        $this->get('/api/memos/nonsense/audio')->assertNotFound();
    }

    private function url(): string
    {
        return '/api/memos/'.self::MEMO_ID.'/audio';
    }

    /**
     * The response body, which for a BinaryFileResponse only exists once it is sent.
     *
     * getContent() answers `false` for one of these -- the payload is a file handle, not a
     * string -- and Laravel's streamedContent() helper is for StreamedResponse only. Sending
     * into an output buffer is what the framework itself does at the end of a request, and it
     * is the only way to see the bytes the offset and length actually produced.
     */
    private function body(TestResponse $response): string
    {
        ob_start();

        try {
            $response->baseResponse->sendContent();
        } finally {
            $body = (string) ob_get_clean();
        }

        return $body;
    }

    /** Deterministic bytes that differ at every offset. See AUDIO_BYTES. */
    private function bytes(int $length): string
    {
        $out = '';

        for ($at = 0; $at < $length; $at++) {
            $out .= chr($at % 256);
        }

        return $out;
    }

    private function voiceMemo(): Memo
    {
        return new Memo(
            id: self::MEMO_ID,
            source: Memo::SOURCE_VOICE,
            status: 'ready',
            transcript: 'Buy milk on the way home.',
            title: null,
            summary: null,
            tags: [],
            durationMs: 4200,
            lastError: null,
            lastErrorCode: null,
            createdAt: '2026-07-31T09:00:00.000Z',
        );
    }

    private function textMemo(): Memo
    {
        return new Memo(
            id: self::MEMO_ID,
            source: Memo::SOURCE_TEXT,
            status: 'ready',
            transcript: 'Typed, so there was never a recording.',
            title: null,
            summary: null,
            tags: [],
            durationMs: null,
            lastError: null,
            lastErrorCode: null,
            createdAt: '2026-07-31T09:00:00.000Z',
        );
    }
}

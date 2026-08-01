<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Contracts\AudioStorage;
use App\Http\Requests\StoreMemoRequest;
use App\Http\Rules\SniffedAudioType;
use App\Repositories\MemoRepository;
use Illuminate\Http\UploadedFile;
use Tests\Support\FakeMemoRepository;
use Tests\Support\RecordingAudioStorage;
use Tests\TestCase;

/**
 * MEMO-11: what happens to an upload this app will not take.
 *
 * Separate from MemoEndpointsTest, which is about the memo a good request produces.
 * Everything here is a refusal, and each one is a refusal with a status and a sentence
 * chosen on purpose -- 413 for a body that was too large, 422 for one that was read and
 * found to be something else, 500 for a failure the person recording cannot fix.
 *
 * **What these tests cannot reach.** php.ini's own limits are applied by PHP while it
 * parses a real multipart body, and nothing in phpunit parses one: an
 * `UploadedFile(test: true)` is handed straight to the request. So `upload_max_filesize`
 * and `post_max_size` are simulated below -- an error code, a Content-Length header --
 * and whether PHP really produces those inputs at those sizes was checked by posting
 * real files at a running container instead. Both halves matter and neither substitutes
 * for the other: the live run is what found the two cases these tests were written
 * around, and these tests are what stop them coming back.
 */
final class AudioUploadEdgeTest extends TestCase
{
    private FakeMemoRepository $repository;

    private RecordingAudioStorage $storage;

    /** Upload temp files created by a test, removed afterwards. @var list<string> */
    private array $temporaryFiles = [];

    protected function setUp(): void
    {
        parent::setUp();

        $this->repository = new FakeMemoRepository;
        $this->app->instance(MemoRepository::class, $this->repository);

        $this->storage = new RecordingAudioStorage;
        $this->app->instance(AudioStorage::class, $this->storage);
    }

    protected function tearDown(): void
    {
        foreach ($this->temporaryFiles as $path) {
            @unlink($path);
        }

        parent::tearDown();
    }

    public function test_a_file_that_is_not_a_recording_is_refused_however_it_is_labelled(): void
    {
        // The acceptance criterion, in the words the task uses: a .txt renamed to .webm
        // is rejected. It arrives named `memo.webm` under `audio/webm` -- both the name
        // and the label say recording, and only the bytes say otherwise.
        $response = $this->post('/api/memos', [
            StoreMemoRequest::AUDIO_FIELD => $this->upload('This is plainly not a recording.'),
        ]);

        $response->assertStatus(422)
            ->assertJsonValidationErrors(StoreMemoRequest::AUDIO_FIELD);

        // 422 and not 415. The request's own content type is multipart/form-data, which
        // this route accepts and read successfully; it is one part *inside* it that is
        // not a recording. 415 would tell the caller to stop sending multipart, which is
        // the one thing they must keep doing.
        $this->assertSame([], $this->repository->inserted);
        $this->assertSame([], $this->storage->written, 'Junk must be refused before anything is written.');

        // The sniffed type is named. The frontend renders this verbatim under the Record
        // button, and "that is not a recording" on its own invites a second attempt with
        // the same file.
        $this->assertStringContainsString('text/plain', (string) $response->json('message'));
    }

    public function test_the_allowlist_covers_containers_no_browser_here_produces(): void
    {
        // The three browser containers are pinned in MemoEndpointsTest. These are the
        // rest of the allowlist -- what someone gets by posting a file by hand, or by a
        // browser this project has not seen. Each was read off this image's libmagic.
        //
        // audio/x-m4a and audio/x-wav are the point: both are `x-` vendor spellings, so
        // an allowlist written from the IANA registry alone would reject a plain wav.
        $containers = [
            'm4a' => ["\x00\x00\x00\x20".'ftypM4A '."\x00\x00\x02\x00".'M4A mp42isom', 'audio/x-m4a'],
            'mp3' => ["\xff\xfb\x90\x64".str_repeat("\x00", 256), 'audio/mpeg'],
            'wav' => ['RIFF'."\x24\x00\x00\x00".'WAVEfmt '."\x10\x00\x00\x00\x01\x00\x01\x00\x80\x3e\x00\x00\x00\x7d\x00\x00\x02\x00\x10\x00".'data'."\x00\x00\x00\x00", 'audio/x-wav'],
        ];

        foreach ($containers as $name => [$bytes, $mime]) {
            $this->post('/api/memos', [StoreMemoRequest::AUDIO_FIELD => $this->upload($bytes)])
                ->assertCreated();

            $this->assertSame($mime, end($this->repository->inserted)['audio_mime'], "{$name}: stored mime");
            $this->assertContains($mime, SniffedAudioType::ALLOWED, "{$name}: on the allowlist");
        }
    }

    public function test_a_recording_at_the_cap_is_stored_and_one_byte_over_is_refused(): void
    {
        // A small cap and small files, so the boundary is tested rather than the machine.
        // The real 12 MiB is checked against a live container instead -- see the class
        // note -- because at that size what is being tested is php.ini, not this branch.
        config(['memo.max_audio_bytes' => 4_096]);

        // Exactly at the cap passes, so this is a cap and not an off-by-one. The same
        // boundary the health endpoint's accepts_max_audio is written against: it asks
        // whether a memo of exactly MAX_AUDIO_BYTES can reach a handler, which is only a
        // useful question if one of exactly that size is accepted.
        $this->post('/api/memos', [StoreMemoRequest::AUDIO_FIELD => $this->recording(4_096)])
            ->assertCreated();

        $this->post('/api/memos', [StoreMemoRequest::AUDIO_FIELD => $this->recording(4_097)])
            ->assertStatus(413);

        $this->assertCount(1, $this->repository->inserted);
        $this->assertCount(1, $this->storage->written, 'The refused recording must not have been written.');
    }

    public function test_a_cap_smaller_than_a_megabyte_is_not_reported_as_zero(): void
    {
        // Found by lowering MAX_AUDIO_BYTES to try the rejection out in a browser: the
        // banner read "That recording is 0.1 MB. The limit is 0.0 MB", which tells the
        // reader their limit is nothing at all. Every cap under about 51 KB rounded to
        // zero, because the message only knew how to write megabytes to one decimal.
        //
        // The same underlying mistake as the collision below -- one fixed format
        // describing a value that is configurable across four orders of magnitude -- so
        // both are pinned together, and neither of them was reachable at the 12 MiB
        // default that every other test here uses.
        config(['memo.max_audio_bytes' => 50_000]);

        $message = (string) $this->post('/api/memos', [
            StoreMemoRequest::AUDIO_FIELD => $this->recording(100_000),
        ])->assertStatus(413)->json('message');

        $this->assertStringContainsString('49 KB', $message);
        $this->assertStringContainsString('98 KB', $message);
        $this->assertStringNotContainsString('0.0 MB', $message);
    }

    public function test_a_recording_barely_over_the_cap_is_not_told_it_is_exactly_the_limit(): void
    {
        // MAX_AUDIO_BYTES + 1 renders both sides of the message identically at one
        // decimal, and the sentence became "That recording is 12.0 MB. The limit is 12.0
        // MB" -- a rejection that reads as a contradiction, found by uploading exactly
        // that against a live container. Reporting the size is only worth doing when the
        // size is different from the limit.
        config(['memo.max_audio_bytes' => 4 * 1024 * 1024]);

        $message = (string) $this->post('/api/memos', [
            StoreMemoRequest::AUDIO_FIELD => $this->recording(4 * 1024 * 1024 + 1),
        ])->assertStatus(413)->json('message');

        $this->assertStringContainsString('4.0 MB', $message);
        $this->assertStringNotContainsString('is 4.0 MB. The limit is 4.0 MB', $message);
    }

    public function test_an_oversized_recording_is_told_its_size_and_the_limit(): void
    {
        config(['memo.max_audio_bytes' => 4 * 1024 * 1024]);

        $response = $this->post('/api/memos', [
            StoreMemoRequest::AUDIO_FIELD => $this->recording(5 * 1024 * 1024),
        ]);

        $response->assertStatus(413);

        $message = (string) $response->json('message');

        // Both numbers, in the same unit, so the sentence can be acted on by somebody
        // who has never seen a byte count. This is the acceptance criterion's "readable
        // message", and it is asserted rather than eyeballed because APP_DEBUG=false
        // replaces the message of anything that is not an HttpException with "Server
        // Error" -- a 413 that lost its wording would still look like a pass.
        $this->assertStringContainsString('5.0 MB', $message);
        $this->assertStringContainsString('4.0 MB', $message);

        $this->assertSame([], $this->repository->inserted);
        $this->assertSame([], $this->storage->written);
    }

    public function test_an_upload_php_refused_for_its_size_is_413_rather_than_a_field_error(): void
    {
        // What a file over `upload_max_filesize` looks like once PHP is done with it: the
        // part survives, the bytes do not, and the only evidence is the error code.
        //
        // Verified against a live container at 17 MB, which is over uploads.ini's 16M and
        // under its 20M post_max_size. Before this branch existed it answered 422 with
        // "The text field is required when audio is not present." as its message -- an
        // instruction to type something, given to somebody who had just uploaded 17 MB.
        $response = $this->post('/api/memos', [
            StoreMemoRequest::AUDIO_FIELD => $this->failedUpload(UPLOAD_ERR_INI_SIZE),
        ]);

        $response->assertStatus(413);

        // No size in the sentence, because there is genuinely no size to report: the
        // upload arrives with size 0 and an empty tmp_name. It still names the limit.
        $this->assertStringContainsString('12.0 MB', (string) $response->json('message'));

        $this->assertSame([], $this->repository->inserted);
    }

    public function test_an_upload_php_could_not_store_is_the_servers_fault_and_not_the_callers(): void
    {
        // A full disk or a missing upload_tmp_dir. Answering 4xx here would tell somebody
        // their recording was the problem when nothing about it was; the same line
        // MemoController draws for an unwritable audio volume, one step earlier.
        foreach ([UPLOAD_ERR_NO_TMP_DIR, UPLOAD_ERR_CANT_WRITE, UPLOAD_ERR_EXTENSION] as $error) {
            $this->post('/api/memos', [StoreMemoRequest::AUDIO_FIELD => $this->failedUpload($error)])
                ->assertStatus(500);
        }

        $this->assertSame([], $this->repository->inserted);
    }

    public function test_a_truncated_upload_stays_a_field_error(): void
    {
        // UPLOAD_ERR_PARTIAL is a connection that dropped mid-body. It is neither too
        // large nor the server's fault, and the honest answer is the `file` rule's --
        // which is also why the size branch lists its codes rather than treating every
        // non-OK error as too large.
        //
        // A real path with real bytes, unlike the failures above: PHP keeps what it
        // received for this one, so getSize() answers with the truncated length rather
        // than false. Checked, because that is what decides which branch sees it -- a
        // partial file under the cap must fall through to the rules, and this test would
        // pass against an empty path whether it did or not.
        $partial = $this->upload(str_repeat("\x00", 1_234));

        $this->assertSame(1_234, $partial->getSize());

        $this->post('/api/memos', [
            StoreMemoRequest::AUDIO_FIELD => new UploadedFile(
                $partial->getPathname(), 'memo.webm', 'audio/webm', UPLOAD_ERR_PARTIAL, test: true
            ),
        ])
            ->assertStatus(422)
            ->assertJsonValidationErrors(StoreMemoRequest::AUDIO_FIELD);

        $this->assertSame([], $this->repository->inserted);
    }

    public function test_an_oversized_body_that_arrived_empty_is_413_whatever_it_claims_to_be(): void
    {
        // PHP discards a body over post_max_size before Laravel boots: $_FILES and $_POST
        // both come back empty with no error flag anywhere. Nothing is left to branch on
        // except the length the client declared -- so a request claiming more bytes than
        // any memo may contain, out of which no file arrived, is answered as the 413 it
        // is rather than as "the audio field is required".
        //
        // The content types are the bug this also closes. The check used to be scoped to
        // multipart/form-data, so a 13 MB body sent as anything else -- which PHP turns
        // into no file and no fields just the same -- answered 422 "The text field is
        // required when audio is not present.", the exact sentence this task exists to
        // stop giving to somebody who uploaded 13 MB. Found by posting one at a live
        // container rather than by reading the branch.
        $bodies = [
            'multipart with a boundary' => 'multipart/form-data; boundary=----boundary',
            'multipart with none' => 'multipart/form-data',
            'form-encoded' => 'application/x-www-form-urlencoded',
        ];

        foreach ($bodies as $name => $contentType) {
            $response = $this->call('POST', '/api/memos', server: [
                'CONTENT_TYPE' => $contentType,
                'CONTENT_LENGTH' => (string) (13 * 1024 * 1024),
            ]);

            $response->assertStatus(413);

            // The declared size, which is also what proves this came from the
            // application's own backstop rather than from Laravel's ValidatePostSize:
            // that one only fires above post_max_size, has no size to report, and would
            // answer without this number. 13 MB is deliberately between the two.
            $this->assertStringContainsString('13.0 MB', (string) $response->json('message'), $name);
        }

        // A header that is absent altogether is the fourth shape and is *not* testable
        // here: Symfony's Request::create substitutes application/x-www-form-urlencoded
        // for a POST that omits it, so this harness cannot produce the request curl sends
        // with no -H at all. Checked against a live container instead, where it answers
        // the same 413 -- and covered here to the extent that it is, because the value it
        // is substituted with is the third case above.

        // JSON keeps its own answer, which is the one exclusion: an oversized typed memo
        // is told which field was too long, and that is more use than a sentence about
        // recordings. bootstrap/app.php pins the same behaviour from its side.
        $this->postJson('/api/memos', ['text' => str_repeat('a', StoreMemoRequest::MAX_TEXT_LENGTH + 1)])
            ->assertStatus(422)
            ->assertJsonValidationErrors('text');

        $this->assertSame([], $this->repository->inserted);
    }

    public function test_the_frameworks_own_413_answers_in_the_same_words(): void
    {
        // Above post_max_size, Laravel's ValidatePostSize answers before any of this
        // application's code runs, and its own message is "The POST data is too large."
        // -- shown, on this route, to somebody who pressed Record. bootstrap/app.php
        // rewords it through the same method the checks above use, so the two paths to a
        // 413 cannot say different things about the same cap.
        $response = $this->call('POST', '/api/memos', server: [
            'CONTENT_TYPE' => 'multipart/form-data; boundary=----boundary',
            'CONTENT_LENGTH' => (string) (256 * 1024 * 1024),
        ]);

        $response->assertStatus(413);

        $message = (string) $response->json('message');

        $this->assertStringContainsString('12.0 MB', $message);
        $this->assertStringNotContainsString('POST data', $message);

        // And the size is *absent*, which is what proves the framework answered rather
        // than rejectDiscardedBody(). Both paths word themselves through the same method
        // and both would satisfy the two assertions above, so without this the test
        // passes whichever one ran -- and the thing it claims to be about is the one that
        // did not.
        $this->assertStringNotContainsString('256.0 MB', $message);
    }

    public function test_an_ordinary_empty_request_is_still_a_field_error(): void
    {
        // The other side of the two tests above, and the one that would break silently:
        // a size check that fires on a small body turns every ordinary mistake into "your
        // recording is too large". A multipart POST that genuinely forgot to attach
        // anything must still be told which field it forgot.
        $this->call('POST', '/api/memos', server: [
            'CONTENT_TYPE' => 'multipart/form-data; boundary=----boundary',
            'CONTENT_LENGTH' => '512',
        ])
            ->assertStatus(422)
            ->assertJsonValidationErrors(['text', StoreMemoRequest::AUDIO_FIELD]);

        // And a request with no Content-Length at all, which is what every JSON request
        // in the suite already is -- the check must not read a missing header as zero and
        // then as something to complain about.
        $this->postJson('/api/memos', [])
            ->assertStatus(422)
            ->assertJsonValidationErrors('text');
    }

    /**
     * A WebM header padded to exactly $bytes, uploaded as `memo.webm`.
     *
     * Padded rather than generated, because finfo reads the magic at the start of a file
     * and nothing after it: the zeroes make the size real without making the sniff
     * anything other than the video/webm a Chrome recording is.
     */
    private function recording(int $bytes): UploadedFile
    {
        $header = "\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1f"
            ."\x42\x86\x81\x01\x42\xf7\x81\x01\x42\xf2\x81\x04\x42\xf3\x81\x08"
            ."\x42\x82\x84webm\x42\x87\x81\x02\x42\x85\x81\x02";

        return $this->upload($header.str_repeat("\x00", $bytes - strlen($header)));
    }

    /**
     * An upload PHP marked failed: a real part, an error code, and no bytes behind it.
     *
     * The empty path is not a shortcut -- it is what PHP actually hands over for every
     * error code here, and it is why StoreMemoRequest reads getError() before it reads
     * getSize() or asks what the file contains.
     */
    private function failedUpload(int $error): UploadedFile
    {
        return new UploadedFile('', 'memo.webm', 'audio/webm', $error, test: true);
    }

    /**
     * A multipart upload of exactly these bytes, always announced as `memo.webm` and
     * `audio/webm` however little that matches what is inside.
     *
     * A real UploadedFile in test mode rather than UploadedFile::fake(), for the reason
     * MemoEndpointsTest gives at length: the fake overrides getMimeType() to guess from
     * the filename, so every test here would pass against it whether the endpoint sniffs
     * or not -- including the one asserting that a text file is refused.
     */
    private function upload(string $contents): UploadedFile
    {
        $path = tempnam(sys_get_temp_dir(), 'memo-upload-');

        $this->assertIsString($path);
        file_put_contents($path, $contents);
        $this->temporaryFiles[] = $path;

        return new UploadedFile($path, 'memo.webm', 'audio/webm', test: true);
    }
}

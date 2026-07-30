<?php

declare(strict_types=1);

namespace Tests\Unit;

use App\Support\Env;
use RuntimeException;
use Tests\TestCase;

/**
 * Both behaviours here are regressions the Slim-to-Laravel port introduced and a
 * revalidation pass caught. They are tested because both were silent: neither
 * produced an error, a log line or a wrong-looking health response.
 */
final class EnvTest extends TestCase
{
    private const KEY = 'MEMO_ENV_TEST_VALUE';

    private const UNSET_KEY = 'MEMO_ENV_TEST_DEFINITELY_UNSET';

    protected function tearDown(): void
    {
        unset($_ENV[self::KEY], $_SERVER[self::KEY]);
        putenv(self::KEY);

        parent::tearDown();
    }

    public function test_a_set_but_empty_variable_falls_back_to_the_default(): void
    {
        // `docker run -e AUDIO_DIR=` and a blank line in a .env both produce this.
        // env()'s own default does not apply, because the variable *is* set -- so
        // LocalAudioStorage would take '' as its root and resolve every key to
        // /<key>, writing audio to the filesystem root of the container.
        $this->set('');

        $this->assertSame('/data/audio', Env::string(self::KEY, '/data/audio'));
        $this->assertSame(123, Env::positiveInt(self::KEY, 123));
    }

    public function test_an_unset_variable_falls_back_to_the_default(): void
    {
        $this->assertSame('/data/audio', Env::string(self::UNSET_KEY, '/data/audio'));
        $this->assertSame(7, Env::positiveInt(self::UNSET_KEY, 7));
    }

    public function test_a_real_value_wins_over_the_default(): void
    {
        $this->set('/mnt/audio');
        $this->assertSame('/mnt/audio', Env::string(self::KEY, '/data/audio'));

        $this->set('4096');
        $this->assertSame(4096, Env::positiveInt(self::KEY, 123));
    }

    public function test_a_non_numeric_byte_cap_throws_rather_than_becoming_zero(): void
    {
        // (int) 'abc' is 0, and a cap of 0 reads as "accepts_max_audio": true on
        // /api/health, because every limit is >= 0. A typo in one variable would
        // disable the byte check MEMO-11 depends on and report itself as fine.
        $this->set('abc');

        $this->expectException(RuntimeException::class);

        Env::positiveInt(self::KEY, 123);
    }

    public function test_a_zero_byte_cap_throws(): void
    {
        // Passes the digit check, so it needs the second guard.
        $this->set('0');

        $this->expectException(RuntimeException::class);

        Env::positiveInt(self::KEY, 123);
    }

    private function set(string $value): void
    {
        $_ENV[self::KEY] = $value;
        $_SERVER[self::KEY] = $value;
        putenv(self::KEY.'='.$value);
    }
}

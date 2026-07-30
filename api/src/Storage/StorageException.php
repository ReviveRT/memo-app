<?php

declare(strict_types=1);

namespace Memo\Storage;

use RuntimeException;

/**
 * A write or delete failed. Distinct from a validation error on purpose: a
 * rejected upload is a 4xx the client can fix, an unwritable volume is a 5xx
 * only an operator can, and the upload edge has to tell them apart.
 */
final class StorageException extends RuntimeException
{
}

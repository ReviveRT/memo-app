<?php

declare(strict_types=1);

namespace App\Exceptions;

use RuntimeException;

/**
 * A blob write or delete failed. Distinct from a validation error on purpose: a
 * rejected upload is a 4xx the client can fix, an unwritable volume is a 5xx only
 * an operator can, and the upload edge (MEMO-11) has to tell them apart.
 */
final class StorageException extends RuntimeException {}

<?php

/**
 * Front controller. The only file under the web root, so nothing else in the
 * image is reachable over HTTP even if the docroot were misconfigured.
 */

declare(strict_types=1);

require __DIR__ . '/../vendor/autoload.php';

Memo\Kernel::createApp()->run();

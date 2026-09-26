<?php

$menuEntries = Array(
	Array(
		'text' => 'Text My Lights',
		'type' => 'content',
		'page' => 'ui.php',
		'wrap' => 1
	),
	Array(
		'text' => 'Text My Lights',
		'type' => 'help',
		'page' => 'help.php',
		'wrap' => 1
	)
);

foreach ($menuEntries as $entry)
{
	if ($entry['type'] != $menu)
		continue;

	$nopage = '';
	if (isset($entry['wrap']) && ($entry['wrap'] == 0))
		$nopage = '&nopage=1';

	printf("<li><a href='plugin.php?plugin=%s&page=%s%s'>%s</a></li>\n",
		$plugin, $entry['page'], $nopage, $entry['text']);
}
?>

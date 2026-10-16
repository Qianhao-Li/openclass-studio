"""Read compatibility for staged, version-bound visual artifacts predating the native reader."""

def positioned_content(data):
    if data.get('format') != 'visual' or data.get('units'):
        return data
    return {**data, 'units': [{'id': index, 'text': '', 'text_start': 0, 'text_end': 0, 'locator': {'kind': 'image_frames', 'start': index, 'end': index}} for index in range(1, data['frame_count'] + 1)]}

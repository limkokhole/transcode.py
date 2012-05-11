#!/usr/bin/env python
# -*- coding: utf-8 -*-

'''
This program uses a MythTV database or WTV recording to transcode
recorded TV programs to MPEG-4 or Matroska video using the H.264/AAC
or VP8/Vorbis/FLAC codecs, cutting the video at certain commercial points
detected by either MythTV or by Comskip. The resulting video file comes
complete with SRT subtitles extracted from the embedded VBI closed-caption
data. and iTunes-compatible metadata about the episode, if it can be found.

In short, this program calls a bunch of programs to convert a file like
1041_20100523000000.mpg or Program Name_ABC_2010_05_23_00_00_00.wtv into a
file like /srv/video/Program Name/Program Name - Episode Name.mp4,
including captions, file tags, chapters, and with commercials removed.

This program has been tested on Windows, Linux and FreeBSD, and can optionally
transcode from remote sources - e.g., mythbackend / MySQL running on a
different computer, or WTV source files from a remote SMB share / HomeGroup.

Requirements:
- FFmpeg (http://ffmpeg.org)
- Java (http://www.java.com)
- Project-X (http://project-x.sourceforge.net)
- remuxTool.jar (http://babgvant.com/downloads/dburckh/remuxTool.jar)
- ccextractor (http://ccextractor.sourceforge.net) (optional)
- Python 2.7 (http://www.python.org)
  - lxml (http://lxml.de)
  - mysql-python (http://sourceforge.net/projects/mysql-python)
  - the MythTV Python bindings (if MythTV is not installed already)

For MPEG-4 / H.264:
- MP4Box (http://gpac.sourceforge.net)
- neroAacEnc (http://www.nero.com/enu/technologies-aac-codec.html) (optional)
- AtomicParsley (https://bitbucket.org/wez/atomicparsley) (optional)
- FFmpeg must be compiled with --enable-libx264
- if using faac for audio, FFmpeg must be compiled with --enable-libfaac

For Matroska / VP8:
- MKVToolNix (http://www.bunkus.org/videotools/mkvtoolnix/)
- FFmpeg must be compiled with --enable-libvpx and --enable-libvorbis

Most of these packages can usually be found in various Linux software
repositories or as pre-compiled Windows binaries. Most precompiled FFmpeg
binaries include support for everything listed above except libfaac.

Setup:
- Make sure the above dependencies are installed on your system. Add each
  binary location to your PATH variable if necessary.
- Edit the settings in transcode.conf to your preference. At a minimum,
  specify paths to Project-X and remuxTool, specify a directory to output
  transcoded video, and enter your MythTV host IP (if using MythTV).
- If on Linux, optionally install transcode.py to /usr/local/bin and
  copy transcode.conf to ~/.transcode - otherwise, run the program from
  within its directory.

Usage:
  transcode.py %CHANID% %STARTTIME%
  transcode.py /path/to/file.wtv
See transcode.py --help for more details

Notes on format string:
Each of the below tags are replaced with the respective metadata obtained
from MythTV, the WTV file, or Tvdb, if it can be found. Path separators
(slashes) indicate that new directories are to be created if necessary.
  %T - title (name of the show)
  %S - subtilte (name of the episode)
  %R - description (short plot synopsis)
  %C - category (genre of the show)
  %n - episode production code (or %s%e if unavailable)
  %s - season number
  %E - episode number (as reported by Tvdb)
  %e - episode number, padded with zeroes if necessary
  %r - content rating (e.g., TV-G)
  %oy - year of original air date (two digits)
  %oY - year of original air date (full four digits)
  %om - month of original air date (two digits)
  %od - day of original air date (two digits)
  
Special thanks to:
- #MythTV Freenode
- wagnerrp, the maintainer of the Python MythTV bindings
- Project-X team
'''

# Changelog:
# 1.3 - support for Matroska / VP8, fixed subtitle sync problems
# 1.2 - better accuracy for commercial clipping, easier configuration
# 1.1 - support for multiple audio streams, Comskip
# 1.0 - initial release

# TODO:
# - better error handling
# - check for adequate disk space on temporary directory and destination
# - generate thumbnail if necessary
# - better genre interpretation for WTV files
# - fetch metadata for movies as well as TV episodes
# - allow users to manually enter in metadata if none can be found

# Long-term goals:
# - add entries to MythTV's database when encoding is finished
# - support for boxed-set TV seasons on DVD
# - metadata / chapter support for as many different players as possible,
#   especially Windows Media Player
# - easier installation: all required programs should be bundled
# - Python 3.x compatibility
# - apply video filters, such as deinterlace, crop detection, etc.
# - a separate program for viewing / editing Comskip data

# Known issues:
# - subtitle font is sometimes too large on QuickTime / iTunes / iPods
# - many Matroska players seem to have trouble displaying metadata properly
# - video_br and video_crf aren't recognized options in ffmpeg 0.7+

import re, os, sys, math, datetime, subprocess, urllib, tempfile, glob
import shutil, codecs, StringIO, time, optparse, unicodedata, logging
import xml.dom.minidom
import MythTV, MythTV.ttvdb.tvdb_api, MythTV.ttvdb.tvdb_exceptions

def _clean(filename):
    'Removes the file if it exists.'
    try:
        os.remove(filename)
    except OSError:
        pass

def _convert_time(time):
    '''Converts a timestamp string into a datetime object.
    For example, '20100523140000' -> datetime(2010, 5, 23, 14, 0, 0) '''
    time = str(time)
    ret = None
    if len(time) != 14:
        raise ValueError('Invalid timestamp - must be 14 digits long.')
    try:
        ret = datetime.datetime.strptime(time, '%Y%m%d%H%M%S')
    except ValueError:
        raise ValueError('Invalid timestamp.')
    return ret

def _convert_timestamp(ts):
    '''Translates the values from a regex match for two timestamps of the
    form 00:12:34,567 into seconds.'''
    start = int(ts.group(1)) * 3600 + int(ts.group(2)) * 60
    start += int(ts.group(3))
    start += float(ts.group(4)) / 10 ** len(ts.group(4))
    end = int(ts.group(5)) * 3600 + int(ts.group(6)) * 60
    end += int(ts.group(7))
    end += float(ts.group(8)) / 10 ** len(ts.group(8))
    return start, end

def _seconds_to_time(sec):
    '''Returns a string representation of the length of time provided.
    For example, 3675.14 -> '01:01:15' '''
    hours = int(sec / 3600)
    sec -= hours * 3600
    minutes = int(sec / 60)
    sec -= minutes * 60
    return '%02d:%02d:%02d' % (hours, minutes, sec)

def _seconds_to_time_frac(sec, comma = False):
    '''Returns a string representation of the length of time provided,
    including partial seconds.
    For example, 3675.14 -> '01:01:15.140000' '''
    hours = int(sec / 3600)
    sec -= hours * 3600
    minutes = int(sec / 60)
    sec -= minutes * 60
    if comma:
        frac = int(round(sec % 1.0 * 1000))
        return '%02d:%02d:%02d,%03d' % (hours, minutes, sec, frac)
    else:
        return '%02d:%02d:%07.4f' % (hours, minutes, sec)

def _last_name_first(name):
    '''Reverses the order of full names of people, so their last name
    appears first.'''
    names = name.split()
    if len(names) == 0:
        return names
    else:
        return u' '.join((names[-1], ' '.join(names[:-1]))).strip()

def _sanitize(filename, repl = None):
    '''Replaces any invalid characters in the filename with repl. Invalid
    characters include anything from \x00 - \x31, and some other characters
    on Windows. UTF-8 characters are translated loosely to ASCII on systems
    which have no Unicode support. Unix systems are assumed to have Unicode
    support. Note that directory separators (slashes) are allowed as they
    specify that a new directory is to be created.'''
    if repl is None:
        repl = ''
    not_allowed = ''
    if os.name == 'nt':
        not_allowed += '<>:|?*'
    out = ''
    for ch in filename:
        if ch not in not_allowed and ord(ch) >= 32:
            if os.name == 'posix' or os.path.supports_unicode_filenames \
                    or ord(ch) < 127:
                out += ch
            else:
                ch = unicodedata.normalize('NFKD', ch)
                ch = ch.encode('ascii', 'ignore')
                out += ch
        else:
            out += repl
    if os.name == 'nt':
        out = out.replace('"', "'")
        reserved = ['con', 'prn', 'aux', 'nul']
        reserved += ['com%d' % d for d in range(0, 10)]
        reserved += ['lpt%d' % d for d in range(0, 10)]
        for r in reserved:
            regex = re.compile(r, re.IGNORECASE)
            out = regex.sub(repl, out)
    return out

def _filter_xml(data):
    'Filters unnecessary whitespace from the raw XML data provided.'
    regex = '<([^/>]+)>\s*(.+)\s*</([^/>]+)>'
    sub = '<\g<1>>\g<2></\g<3>>'
    return re.sub(regex, sub, data)
    
def _cmd(args, cwd = None, expected = 0):
    '''Executes an external command with the given working directory, ignoring
    all output. Raises a RuntimeError exception if the return code of the
    subprocess isn't what is expected.'''
    args = [unicode(arg).encode('utf_8') for arg in args]
    ret = 0
    logging.debug('$ %s' % u' '.join(args))
    time.sleep(.25)
    proc = subprocess.Popen(args, stdout = subprocess.PIPE,
                            stderr = subprocess.STDOUT, cwd = cwd)
    for line in proc.stdout:
        logging.debug(line.replace('\n', ''))
    ret = proc.wait()
    time.sleep(.25)
    if ret != 0 and ret != expected:
        raise RuntimeError('Unexpected return code', u' '.join(args), ret)
    return ret

def _ver(args, regex, use_stderr = True):
    '''Executes an external command and searches through the standard output
    (and optionally stderr) for the provided regular expression. Returns the
    first matched group, or None if no matches were found.'''
    ret = None
    with open(os.devnull, 'w') as devnull:
        try:
            stderr = subprocess.STDOUT
            if not use_stderr:
                stderr = devnull
            proc = subprocess.Popen(args, stdout = subprocess.PIPE,
                                    stderr = stderr)
            for line in proc.stdout:
                match = re.search(regex, line)
                if match:
                    ret = match.group(1).strip()
                    break
            for line in proc.stdout:
                pass
            proc.wait()
        except OSError:
            pass
    return ret

def _nero_ver():
    '''Determines whether neroAacEnc is present, and if so, returns the
    version string for later.'''
    regex = 'Package version:\s*([0-9.]+)'
    ver = _ver(['neroAacEnc', '-help'], regex)
    if not ver:
        regex = 'Package build date:\s*([A-Za-z]+\s+[0-9]+\s+[0-9]+)'
        ver = _ver(['neroAacEnc', '-help'], regex)
    if ver:
        ver = 'Nero AAC Encoder %s' % ver
    return ver

def _ffmpeg_ver():
    '''Determines whether ffmpeg is present, and if so, returns the
    version string for later.'''
    return _ver(['ffmpeg', '-version'], '([Ff]+mpeg.*)$',
                use_stderr = False)

def _x264_ver():
    '''Determines whether x264 is present, and if so, returns the
    version string for later. Does not check whether ffmpeg has libx264
    support linked in.'''
    return _ver(['x264', '--version'], '(x264.*)$')

def _faac_ver():
    '''Determines whether ffmpeg is present, and if so, returns the
    version string for later. Does not check whether ffmpeg has libfaac
    support linked in.'''
    return _ver(['faac', '--help'], '(FAAC.*)$')

def _projectx_ver(opts):
    '''Determines whether Project-X is present, and if so, returns the
    version string for later.'''
    return _ver(['java', '-jar', opts.projectx, '-?'],
                '(ProjectX [0-9/.]*)')

def _version(opts):
    'Compiles a string representing the versions of the encoding tools.'
    nero_ver = _nero_ver()
    faac_ver = _faac_ver()
    x264_ver = _x264_ver()
    ver = _ffmpeg_ver()
    if x264_ver:
        ver += ', %s' % x264_ver
    ver += ', %s' % _projectx_ver(opts)
    if opts.nero and nero_ver:
        ver += ', %s' % nero_ver
    elif faac_ver:
        ver += ', %s' % faac_ver
    logging.debug('Version string: %s' % ver)
    return ver

def _iso_639_2(lang):
    '''Translates from a two-letter ISO language code (ISO 639-1) to a
    three-letter ISO language code (ISO 639-2).'''
    languages = {'cs' : 'ces', 'da' : 'dan', 'de' : 'deu', 'es' : 'spa',
                 'el' : 'ell', 'en' : 'eng', 'fi' : 'fin', 'fr' : 'fra',
                 'he' : 'heb', 'hr' : 'hrv', 'hu' : 'hun', 'it' : 'ita',
                 'ja' : 'jpn', 'ko' : 'kor', 'nl' : 'nld', 'no' : 'nor',
                 'pl' : 'pol', 'pt' : 'por', 'ru' : 'rus', 'sl' : 'slv',
                 'sv' : 'swe', 'tr' : 'tur', 'zh' : 'zho'}
    if lang in languages.keys():
        return languages[lang]
    else:
        raise ValueError('Invalid language code %s.' % lang)

def _find_conf_file():
    'Obtains the location of the config file if one exists.'
    local_dotfile = os.path.expanduser('~/.transcode')
    if os.path.exists(local_dotfile):
        return local_dotfile
    conf_file = os.path.split(os.path.realpath(__file__))[0]
    conf_file = os.path.join(conf_file, 'transcode.conf')
    if os.path.exists(conf_file):
        return conf_file
    return None

def _get_defaults():
    'Returns configuration defaults for this program.'
    opts = {'final_path' : '/srv/video', 'tmp' : None, 'matroska' : False,
            'format' : '%T/%T - %S', 'replace_char' : '', 'language' : 'en',
            'vp8' : False, 'two_pass' : False, 'h264_preset' : None,
            'vp8_preset' : '720p', 'video_br' : 1500, 'video_crf' : 22,
            'resolution' : None, 'ipod' : False, 'webm' : False,
            'ipod_resolution' : '480p', 'ipod_preset' : 'ipod640',
            'webm_resolution' : '720p', 'webm_preset' : '720p', 'nero' : True,
            'flac' : False, 'audio_br' : 128, 'audio_q' : 0.3,
            'use_tvdb_rating' : True, 'use_tvdb_descriptions' : False,
            'host' : '127.0.0.1', 'database' : 'mythconverg',
            'user' : 'mythtv', 'password' : 'mythtv', 'pin' : 0,
            'quiet' : False, 'verbose' : False, 'thresh' : 5,
            'projectx' : 'project-x/ProjectX.jar',
            'remuxtool' : 'remuxTool.jar'}
    return opts

def _add_option(opts, key, val):
    'Inserts the given configuration setting into the dictionary.'
    key = key.lower()
    if key in ['tmp', 'h264_preset', 'vp8_preset', 'resolution',
               'ipod_preset', 'ipod_resolution', 'webm_preset',
               'webm_resolution']:
        if val == '' or not val:
            val = None
    if key in ['matroska', 'vp8', 'two_pass', 'webm', 'ipod', 'nero', 'flac',
               'use_tvdb_rating', 'use_tvdb_descriptions', 'quiet', 'verbose']:
        val = val.lower()
        if val in ['1', 't', 'y', 'true', 'yes', 'on']:
            val = True
        elif val in ['0', 'f', 'n', 'false', 'no', 'off']:
            val = False
        else:
            raise ValueError('Invalid boolean value for %s: %s' % (key, val))
    if key in ['video_br', 'video_crf', 'audio_br', 'audio_q',
               'pin', 'thresh', 'audio_q']:
        try:
            if key == 'audio_q':
                val = float(val)
            else:
                val = int(val)
        except ValueError:
            raise ValueError('Invalid numerical value for %s: %s' % (key, val))
    if key in ['projectx', 'remuxtool']:
        if not os.path.exists(val):
            raise IOError('File not found: %s' % val)
    opts[key] = val

def _read_options():
    'Reads configuration settings from a file.'
    opts = _get_defaults()
    conf_name = _find_conf_file()
    if conf_name is not None:
        with open(conf_name, 'r') as conf_file:
            regex = re.compile('^\s*(.*)\s*=\s*(.*)\s*$')
            ignore = re.compile('#.*')
            for line in conf_file:
                line = re.sub(ignore, '', line).strip()
                match = re.search(regex, line)
                if match:
                    _add_option(opts, match.group(1).strip(),
                                match.group(2).strip())
    return opts

def _def_str(test, val):
    'Returns " [default]" if test is equal to val.'
    if test == val:
        return ' [default]'
    else:
        return ''

def _get_options():
    'Uses optparse to obtain command-line options.'
    opts = _read_options()
    usage = 'usage: %prog [options] chanid time\n' + \
        '  %prog [options] wtv-file'
    version = '%prog 1.3'
    parser = optparse.OptionParser(usage = usage, version = version,
                                   formatter = optparse.TitledHelpFormatter())
    flopts = optparse.OptionGroup(parser, 'File options')
    flopts.add_option('-o', '--out', dest = 'final_path', metavar = 'PATH',
                      default = opts['final_path'], help = 'directory to ' +
                      'store encoded video file                          ' +
                      '[default: %default]')
    if opts['tmp']:
        flopts.add_option('-t', '--tmp', dest = 'tmp', metavar = 'PATH',
                          default = opts['tmp'], help = 'temporary ' +
                          'directory to be used while transcoding ' +
                          '[default: %default]')
    else:
        flopts.add_option('-t', '--tmp', dest = 'tmp', metavar = 'PATH',
                          help = 'temporary directory to be used while ' +
                          'transcoding [default: %s]' % tempfile.gettempdir())
    flopts.add_option('--format', dest = 'format',
                      default = opts['format'], metavar = 'FMT',
                      help = 'format string for the encoded video filename ' +
                      '          [default: %default]')
    flopts.add_option('--replace', dest = 'replace_char',
                      default = opts['replace_char'],  metavar = 'CHAR',
                      help = 'character to substitute for invalid filename ' +
                      'characters [default: "%default"]')
    flopts.add_option('-l', '--lang', dest = 'language',
                      default = opts['language'], metavar = 'LANG',
                      help = 'two-letter language code [default: %default]')
    parser.add_option_group(flopts)
    vfopts = optparse.OptionGroup(parser, 'Video format options')
    vfopts.add_option('--mp4', dest = 'matroska', action = 'store_false',
                      help = 'use the MPEG-4 (MP4 / M4V) container format' +
                      _def_str(opts['matroska'], False))
    vfopts.add_option('--mkv', dest = 'matroska', action = 'store_true',
                      default = opts['matroska'], help = 'use the Matroska ' +
                      '(MKV) container format' +
                      _def_str(opts['matroska'], True))
    vfopts.add_option('--h264', dest = 'vp8', action = 'store_false',
                      default = opts['vp8'], help = 'use H.264/MPEG-4 AVC ' +
                      'and AAC' + _def_str(opts['vp8'], False))
    vfopts.add_option('--vp8', dest = 'vp8', action = 'store_true',
                      default = opts['vp8'], help = 'use On2\'s VP8 codec ' +
                      'and Ogg Vorbis' + _def_str(opts['vp8'], True))
    vfopts.add_option('--ipod', dest = 'ipod', action = 'store_true',
                      default = opts['ipod'], help = 'use iPod Touch ' +
                      'compatibility settings' + _def_str(opts['ipod'], True))
    vfopts.add_option('--no-ipod', dest = 'ipod', action = 'store_false',
                      help = 'do not use iPod compatibility settings' +
                      _def_str(opts['ipod'], False))
    vfopts.add_option('--webm', dest = 'webm', action = 'store_true',
                      default = opts['webm'], help = 'encode WebM ' +
                      'compliant video' + _def_str(opts['webm'], True))
    vfopts.add_option('--no-webm', dest = 'webm', action = 'store_false',
                      help = 'do not encode WebM compliant video' +
                      _def_str(opts['webm'], False))
    parser.add_option_group(vfopts)
    viopts = optparse.OptionGroup(parser, 'Video encoding options')
    viopts.add_option('-1', '--one-pass', dest = 'two_pass',
                      action = 'store_false', default = opts['two_pass'],
                      help = 'one-pass encoding' +
                      _def_str(opts['two_pass'], False))
    viopts.add_option('-2', '--two-pass', dest = 'two_pass',
                      action = 'store_true', help = 'two-pass encoding' +
                      _def_str(opts['two_pass'], True))
    viopts.add_option('--video-br', dest = 'video_br', metavar = 'BR',
                      type = 'int', default = opts['video_br'],
                      help = 'two-pass target video bitrate (in KB/s) ' +
                      '[default: %default]')
    viopts.add_option('--video-crf', dest = 'video_crf', metavar = 'CR',
                      type = 'int', default = opts['video_crf'],
                      help = 'one-pass target compression ratio (~15-25 is ' +
                      'ideal) [default: %default]')
    viopts.add_option('--h264-preset', dest = 'h264_preset', metavar = 'PRE',
                      default = opts['h264_preset'], help = 'ffmpeg x264 ' +
                      'preset to use [default: %default]')
    viopts.add_option('--vp8-preset', dest = 'vp8_preset', metavar = 'PRE',
                      default = opts['vp8_preset'], help = 'ffmpeg libvpx ' +
                      'preset to use [default: %default]')
    viopts.add_option('-r', '--res', dest = 'resolution', metavar = 'RES',
                      default = opts['resolution'], help = 'target video ' +
                      'resolution or aspect ratio [default: %default]')
    viopts.add_option('--ipod-preset', dest = 'ipod_preset', metavar = 'PRE',
                      default = opts['ipod_preset'], help = 'ffmpeg x264 ' +
                      'preset to use for iPod Touch compatibility ' +
                      '[default: %default]')
    viopts.add_option('--ipod-res', dest = 'ipod_resolution', metavar = 'RES',
                      default = opts['ipod_resolution'], help = 'target ' +
                      'video resolution for iPod Touch compatibility ' +
                      '[default: %default]')
    viopts.add_option('--webm-preset', dest = 'webm_preset', metavar = 'PRE',
                      default = opts['webm_preset'], help = 'ffmpeg libvpx ' +
                      'preset to use for WebM video ' +
                      '[default: %default]')
    viopts.add_option('--webm-res', dest = 'webm_resolution', metavar = 'RES',
                      default = opts['webm_resolution'], help = 'target ' +
                      'video resolution for WebM video ' +
                      '[default: %default]')
    parser.add_option_group(viopts)
    auopts = optparse.OptionGroup(parser, 'Audio encoding options')
    auopts.add_option('--nero', dest = 'nero', action = 'store_true',
                      default = opts['nero'], help = 'use NeroAacEnc (must ' +
                      'be installed)' + _def_str(opts['nero'], True))
    auopts.add_option('--faac', dest = 'nero', action = 'store_false',
                      help = 'use libfaac (must be linked into ffmpeg)' +
                      _def_str(opts['nero'], False))
    auopts.add_option('--vorbis', dest = 'flac', action = 'store_false',
                      default = opts['flac'], help = 'use Ogg Vorbis audio ' +
                      '(MKV only)' + _def_str(opts['flac'], False))
    auopts.add_option('--flac', dest = 'flac', action = 'store_true',
                      help = 'use FLAC audio (MKV only)' +
                      _def_str(opts['flac'], True))
    auopts.add_option('--audio-br', dest = 'audio_br', metavar = 'BR',
                      type = 'int', default = opts['audio_br'], help =
                      'faac / vorbis audio bitrate (in KB/s) ' +
                      '[default: %default]')
    auopts.add_option('--audio-q', dest = 'audio_q', metavar = 'Q',
                      type = 'float', default = opts['audio_q'], help =
                      'neroAacEnc audio quality ratio [default: %default]')
    parser.add_option_group(auopts)
    mdopts = optparse.OptionGroup(parser, 'Metadata options')
    mdopts.add_option('--rating', dest = 'use_tvdb_rating',
                      action = 'store_true', default = opts['use_tvdb_rating'],
                      help = 'include Tvdb episode rating (1 to 10) ' +
                      'as voted by users' +
                      _def_str(opts['use_tvdb_rating'], True))
    mdopts.add_option('--no-rating', dest = 'use_tvdb_rating',
                      action = 'store_false', help = 'do not include Tvdb ' +
                      'episode rating' +
                      _def_str(opts['use_tvdb_rating'], False))
    mdopts.add_option('--tvdb-description', dest = 'use_tvdb_descriptions',
                      action = 'store_true',
                      default = opts['use_tvdb_descriptions'], help = 'use ' +
                      'episode descriptions from Tvdb when available' +
                      _def_str(opts['use_tvdb_descriptions'], True))
    parser.add_option_group(mdopts)
    myopts = optparse.OptionGroup(parser, 'MythTV options')
    myopts.add_option('--host', dest = 'host', metavar = 'IP',
                      default = opts['host'], help = 'MythTV database ' +
                      'host [default: %default]')
    myopts.add_option('--database', dest = 'database', metavar = 'DB',
                      default = opts['database'], help = 'MySQL database ' +
                      'for MythTV [default: %default]')
    myopts.add_option('--user', dest = 'user', metavar = 'USER',
                      default = opts['user'], help = 'MySQL username for ' +
                      'MythTV [default: %default]')
    myopts.add_option('--password', dest = 'password', metavar = 'PWD',
                      default = opts['password'], help = 'MySQL password ' +
                      'for MythTV [default: %default]')
    myopts.add_option('--pin', dest = 'pin', metavar = 'PIN', type = 'int',
                      default = opts['pin'], help = 'MythTV security PIN ' +
                      '[default: %04d]' % opts['pin'])
    parser.add_option_group(myopts)
    miopts = optparse.OptionGroup(parser, 'Miscellaneous options')
    miopts.add_option('-q', '--quiet', dest = 'quiet', action = 'store_true',
                      default = opts['quiet'], help = 'avoid printing to ' +
                      'stdout' + _def_str(opts['quiet'], True))
    miopts.add_option('-v', '--verbose', dest = 'verbose',
                      action = 'store_true', default = opts['verbose'],
                      help = 'print command output to stdout' +
                      _def_str(opts['verbose'], True))
    miopts.add_option('--thresh', dest = 'thresh', metavar = 'TH',
                      type = 'int', default = opts['thresh'], help = 'ignore ' +
                      'clip segments TH seconds from the beginning or end ' +
                      '[default: %default]')
    miopts.add_option('--project-x', dest = 'projectx', metavar = 'PATH',
                      default = opts['projectx'], help = 'path to the ' +
                      'Project-X JAR file                            ' +
                      '(used for noise cleaning / cutting)')
    miopts.add_option('--remuxtool', dest = 'remuxtool', metavar = 'PATH',
                      default = opts['remuxtool'], help = 'path to ' +
                      'remuxTool.jar                               ' +
                      '(used for extracting MPEG-2 data from WTV files)')
    parser.add_option_group(miopts)
    return parser

def _check_args(args, parser, opts):
    '''Checks to ensure the positional arguments are valid, and adjusts
    conflicting options if necessary.'''
    if len(args) == 2:
        try:
            ts = _convert_time(args[1])
        except ValueError:
            print 'Error: invalid timestamp.'
            exit(1)
    elif len(args) == 1:
        if not os.path.exists(args[0]):
            print 'Error: file not found.'
            exit(1)
        if not re.search('\.[Ww][Tt][Vv]', args[0]):
            print 'Error: file is not a WTV recording.'
            exit(1)
    else:
        parser.print_help()
        exit(1)
    if opts.ipod and opts.webm:
        print 'Error: WebM and iPod options conflict.'
        exit(1)
    if (opts.vp8 or opts.flac or opts.webm) and not opts.matroska:
        opts.matroska = True
    if opts.ipod:
        opts.matroska = False
        opts.vp8 = False
        opts.flac = False
        opts.resolution = opts.ipod_resolution
        opts.preset = opts.ipod_preset
    elif opts.webm:
        opts.matroska = True
        opts.vp8 = True
        opts.flac = False
        opts.resolution = opts.webm_resolution
        opts.preset = opts.webm_preset
    elif opts.vp8:
        opts.preset = opts.vp8_preset
    else:
        opts.preset = opts.h264_preset
    loglvl = logging.INFO
    if opts.verbose:
        loglvl = logging.DEBUG
    if opts.quiet:
        loglvl = logging.CRITICAL
    logging.basicConfig(format = '%(message)s', level = loglvl)

class Subtitles:
    '''Extracts closed captions from source media using ccextractor and
    writes them as SRT timed-text subtitles.'''
    subs = 1
    marks = []
    
    def __init__(self, source):
        self.source = source
        self.srt = source.base + '.srt'
        self.enabled = self.check()
        if not self.enabled:
            logging.warning('*** ccextractor not found, ' +
                            'subtitles disabled ***')
    
    def check(self):
        '''Determines whether ccextractor is present, and if so,
        stores the version number for later.'''
        ver = _ver(['ccextractor'], '(CCExtractor [0-9]+\.[0-9]+),')
        return ver is not None and not self.source.opts.webm
    
    def mark(self, sec):
        'Marks a cutpoint in the video at a given point.'
        self.marks += [sec]
    
    def extract(self, video):
        '''Obtains the closed-caption data embedded as VBI data within the
        filtered video clip and writes them to a SRT file.'''
        if not self.enabled:
            return
        logging.info('*** Extracting subtitles ***')
        _cmd(['ccextractor', '-o', self.srt, '-utf8', '-ve',
              '--no_progress_bar', video], expected = 232)
    
    def adjust(self):
        '''Joining video can cause the closed-caption VBI data to become
        out-of-sync with the video, because some closed captions last longer
        than the individual clips and ccextractor doesn't know when to clip
        these captions. To compensate for this, any captions which
        extend longer than the cutpoint are clipped, and the difference is
        subtracted from the rest of the captions.'''
        if len(self.marks) == 0:
            return
        delay = 0.0
        curr = 0
        newsubs = ''
        ts = '(\d\d):(\d\d):(\d\d),(\d+)'
        regex = re.compile(ts + '\s*-+>\s*' + ts)
        with open(self.srt, 'r') as subs:
            for line in subs:
                match = re.search(regex, line)
                if match:
                    (start, end) = _convert_timestamp(match)
                    start += delay
                    end += delay
                    if curr < len(self.marks) and self.marks[curr] < end:
                        if self.marks[curr] > start:
                            delay += self.marks[curr] - end
                            end = self.marks[curr]
                        curr += 1
                    start = _seconds_to_time_frac(start, True)
                    end = _seconds_to_time_frac(end, True)
                    newsubs += '%s --> %s\n' % (start, end)
                else:
                    newsubs += line
        _clean(self.srt)
        with open(self.srt, 'w') as subs:
            subs.write(newsubs)
    
    def clean_tmp(self):
        'Removes temporary SRT files.'
        _clean(self.srt)

class MP4Subtitles(Subtitles):
    'Embeds SRT subtitles into the final MPEG-4 video file.'
    
    def write(self):
        'Invokes MP4Box to embed the SRT subtitles into a MP4 file.'
        if not self.enabled:
            return
        arg = '%s:name=Subtitles:layout=0x125x0x-1' % self.srt
        _cmd(['MP4Box', '-tmp', self.source.opts.final_path, '-add',
              arg, self.source.mp4])

class MKVSubtitles(Subtitles):
    'Embeds SRT subtitles into the final MPEG-4 video file.'
    
    def write(self):
        '''Returns command-line arguments for mkvmerge to embed the SRT
        subtitles into a MKV file.'''
        return ['--track-name', '0:Subtitles', self.srt]

class MP4Chapters:
    'Creates iOS-style chapter markers between designated cutpoints.'
    
    def __init__(self, source):
        self.source = source
        self.enabled = not self.source.opts.webm
        self._chap = source.base + '.xml'
        doc = xml.dom.minidom.Document()
        doc.appendChild(doc.createComment('GPAC 3GPP Text Stream'))
        stream = doc.createElement('TextStream')
        stream.setAttribute('version', '1.1')
        doc.appendChild(stream)
        header = doc.createElement('TextStreamHeader')
        stream.appendChild(header)
        sample = doc.createElement('TextSampleDescription')
        header.appendChild(sample)
        sample.appendChild(doc.createElement('FontTable'))
        self._doc = doc
    
    def add(self, pos, seg):
        '''Adds a new chapter marker at pos (in seconds).
        If seg is provided, the marker will be labelled 'Scene seg+1'.'''
        sample = self._doc.createElement('TextSample')
        sample.setAttribute('sampleTime', _seconds_to_time_frac(round(pos)))
        if seg is None:
            sample.setAttribute('text', '')
        else:
            text = self._doc.createTextNode('Scene %d' % (seg + 1))
            sample.appendChild(text)
            sample.setAttribute('xml:space', 'preserve')
        self._doc.documentElement.appendChild(sample)
    
    def write(self):
        '''Outputs the chapter data to XML format and invokes MP4Box to embed
        the chapter XML file into a MP4 file.'''
        if not self.enabled:
            return
        _clean(self._chap)
        data = self._doc.toprettyxml(encoding = 'UTF-8', indent = '  ')
        data = _filter_xml(data)
        logging.debug('Chapter XML file:')
        logging.debug(data)
        with open(self._chap, 'w') as dest:
            dest.write(data)
        args = ['MP4Box', '-tmp', self.source.opts.final_path,
                '-add', '%s:chap' % self._chap, self.source.mp4]
        try:
            _cmd(args)
        except RuntimeError:
            logging.warning('*** Old version of MP4Box, ' +
                            'chapter support unavailable ***')
    
    def clean_tmp(self):
        'Removes the temporary chapter XML file.'
        _clean(self._chap)

class MKVChapters:
    '''Creates a simple-format Matroska chapter file and embeds it into
    the final MKV media file.'''
    
    def __init__(self, source):
        self.source = source
        self.enabled = not source.opts.webm
        self._chap = source.base + '.chap'
        self._data = ''
    
    def add(self, pos, seg):
        '''Adds a new chapter marker at pos (in seconds).
        If seg is provided, the marker will be labelled 'Scene seg+1'.'''
        if seg is None or not self.enabled:
            return
        time = _seconds_to_time_frac(pos)
        self._data += 'CHAPTER%02d=%s\n' % (seg, time)
        self._data += 'CHAPTER%02dNAME=%s\n' % (seg, 'Scene %d' % (seg + 1))
    
    def write(self):
        '''Writes the chapter file and returns command-line arguments to embed
        the chapter date into a MKV file.'''
        if not self.enabled:
            return []
        _clean(self._chap)
        logging.debug('Chapter data file:')
        logging.debug(self._data)
        if len(self._data) > 0:
            with open(self._chap, 'w') as dest:
                dest.write(self._data)
            return ['--chapters', self._chap]
        else:
            return []
    
    def clean_tmp(self):
        'Removes the temporary chapter file.'
        _clean(self._chap)

class MP4Metadata:
    '''Translates previously fetched metadata (series name, episode name,
    episode number, credits...) into command-line arguments for AtomicParsley
    in order to embed it as iOS-compatible MP4 tags.'''
    _rating_ok = False
    _credits_ok = False
    
    def __init__(self, source):
        self.source = source
        self.enabled = self.check()
        if not self.enabled:
            logging.warning('*** AtomicParsley not found, ' +
                            'metadata disabled ***')
        elif not self._rating_ok or not self._credits_ok:
            logging.warning('*** Old version of AtomicParsley, ' +
                            'some functions unavailable ***')
    
    def check(self):
        '''Determines whether AtomicParsley is present, and if so, checks
        whether code to handle 'reverse DNS' style atoms (needed for content
        ratings and embedded XML credits) is present.'''
        ver = _ver(['AtomicParsley', '--help'], '(AtomicParsley)')
        if ver:
            rdns = ['AtomicParsley', '--reverseDNS-help']
            if _ver(rdns, '.*(--contentRating)'):
                self._rating_ok = True
            if _ver(rdns, '.*(--rDNSatom)'):
                self._credits_ok = True
            return not self.source.opts.webm
        else:
            return False
    
    def _sort_credits(self):
        '''Browses through the previously obtained list of credited people
        involved with the episode and returns three separate lists of
        actors/hosts/guest stars, directors, and producers/writers.'''
        cast = []
        directors = []
        producers = []
        c = self.source.get('credits')
        for person in c:
            if person[1] in ['actor', 'host', '']:
                cast.append(person[0])
            elif person[1] == 'director':
                directors.append(person[0])
            elif person[1] == 'executive_producer':
                producers.append(person[0])
        for person in c:
            if person[1] == 'guest_star':
                cast.append(person[0])
            elif person[1] == 'producer':
                producers.append(person[0])
        for person in c:
            if person[1] == 'writer':
                producers.append(person[0])
        return cast, directors, producers
    
    def _make_section(self, people, key_name, xml, doc):
        '''Creates an XML branch named key_name, adds each person contained
        in people to an array within it, and appends the branch to xml, using
        the XML document doc.'''
        if len(people) > 0:
            key = doc.createElement('key')
            key.appendChild(doc.createTextNode(key_name))
            xml.appendChild(key)
            array = doc.createElement('array')
            xml.appendChild(array)
            for person in people:
                dic = doc.createElement('dict')
                key = doc.createElement('key')
                key.appendChild(doc.createTextNode('name'))
                dic.appendChild(key)
                name = doc.createElement('string')
                name.appendChild(doc.createTextNode(person))
                dic.appendChild(name)
                array.appendChild(dic)
    
    def _make_credits(self):
        '''Returns an iOS-compatible XML document listing the actors, directors
        and producers involved with the episode.'''
        (cast, directors, producers) = self._sort_credits()
        imp = xml.dom.minidom.getDOMImplementation()
        dtd = '-//Apple Computer//DTD PLIST 1.0//EN'
        url = 'http://www.apple.com/DTDs/PropertyList-1.0.dtd'
        dt = imp.createDocumentType('plist', dtd, url)
        doc = imp.createDocument(url, 'plist', dt)
        root = doc.documentElement
        root.setAttribute('version', '1.0')
        top = doc.createElement('dict')
        root.appendChild(top)
        self._make_section(cast, 'cast', top, doc)
        self._make_section(directors, 'directors', top, doc)
        self._make_section(producers, 'producers', top, doc)
        return doc
    
    def _perform(self, args):
        '''Invokes AtomicParsley with the provided command-line arguments. If
        the program is successful and outputs a new MP4 file with the newly
        embedded metadata, copies the new file over the old.'''
        self.clean_tmp()
        args = ['AtomicParsley', self.source.mp4] + args
        _cmd(args)
        for old in glob.glob(self.source.final + '-temp-*.' +
                             self.source.mp4ext):
            shutil.move(old, self.source.mp4)
    
    def _simple_tags(self, version):
        'Adds single-argument or standalone tags into the MP4 file.'
        s = self.source
        args = ['--stik', 'TV Show', '--encodingTool', version,
                '--grouping', 'MythTV Recording']
        if s.get('time') is not None:
            utc = s.time.strftime('%Y-%m-%dT%H:%M:%SZ')
            args += ['--purchaseDate', utc]
        if s.get('title') is not None:
            t = s['title']
            args += ['--artist', t, '--album', t, '--albumArtist', t,
                     '--TVShowName', t]
        if s.get('subtitle') is not None:
            args += ['--title', s['subtitle']]
        if s.get('category') is not None:
            args += ['--genre', s['category']]
        if s.get('originalairdate') is not None:
            args += ['--year', str(s['originalairdate'])]
        if s.get('channel') is not None:
            args += ['--TVNetwork', s['channel']]
        if s.get('syndicatedepisodenumber') is not None:
            args += ['--TVEpisode', s['syndicatedepisodenumber']]
        if s.get('episode') is not None and \
                s.get('episodecount') is not None:
            track = '%s/%s' % (s['episode'], s['episodecount'])
            args += ['--tracknum', track, '--TVEpisodeNum', s['episode']]
        if s.get('season') is not None and \
                s.get('seasoncount') is not None:
            disk = '%s/%s' % (s['season'], s['seasoncount'])
            args += ['--disk', disk, '--TVSeasonNum', s['season']]
        if s.get('rating') is not None and self._rating_ok:
            args += ['--contentRating', s['rating']]
        self._perform(args)
    
    def _longer_tags(self):
        '''Adds lengthier tags (such as episode description or series artwork)
        into the MP4 file separately, to avoid problems.'''
        s = self.source
        args = []
        if s.get('description') is not None:
            args += ['--description', s['description']]
        self._perform(args)
        if s.get('albumart') is not None:
            try:
                self._perform(['--artwork', s['albumart']])
            except RuntimeError:
                logging.warning('*** Could not embed artwork ***')
    
    def _credits(self):
        'Adds the credits XML data directly as a command-line argument.'
        c = self.source.get('credits')
        if c is not None and c != [] and self._credits_ok:
            doc = self._make_credits()
            args = ['--rDNSatom', doc.toxml(encoding = 'UTF-8'),
                    'name=iTunMOVI', 'domain=com.apple.iTunes']
            self._perform(args)
    
    def write(self, version):
        '''Performs each of the above steps involved in embedding metadata,
        using version as the encodingTool tag.'''
        if self.enabled:
            logging.info('*** Adding metadata to %s ***' % self.source.mp4)
            self._simple_tags(version)
            self._longer_tags()
            self._credits()
    
    def clean_tmp(self):
        'Removes any leftover MP4 files created by AtomicParsley.'
        files = u'%s-temp-*.%s' % (self.source.final, self.source.mp4ext)
        for old in glob.glob(files):
            _clean(old)

class MKVMetadata:
    '''Translates previously fetched metadata (series name, episode name,
    episode number, credits...) into an XML tags file for mkvmerge
    in order to embed it as Matroska tags.'''
    
    def __init__(self, source):
        self.source = source
        self.enabled = not self.source.opts.webm
        self._tags = self.source.base + '-tags.xml'
        imp = xml.dom.minidom.getDOMImplementation()
        url = 'http://www.matroska.org/files/tags/matroskatags.dtd'
        dt = imp.createDocumentType('Tags', None, url)
        self._doc = imp.createDocument(url, 'Tags', dt)
        self._root = self._doc.documentElement
        self._show = self._make_tag('collection', 70)
        self._season = self._make_tag('season', 60)
        self._ep = self._make_tag('episode', 50)
    
    def _make_tag(self, targtype, targval):
        '''Creates an XML branch for Matroska tags using the desired level
        of specificity to which the included tags will apply.'''
        tag = self._doc.createElement('Tag')
        targ = self._doc.createElement('Targets')
        tv = self._doc.createElement('TargetTypeValue')
        tv.appendChild(self._doc.createTextNode(str(targval)))
        targ.appendChild(tv)
        tt = self._doc.createElement('TargetType')
        tt.appendChild(self._doc.createTextNode(targtype.upper()))
        targ.appendChild(tt)
        tag.appendChild(targ)
        self._root.appendChild(tag)
        return tag
    
    def _add_simple(self, tag, name, val):
        '''Creates an XML Matroska <Simple> tag with the specified name
        and value and attaches it to the given tag.'''
        simple = self._doc.createElement('Simple')
        n = self._doc.createElement('Name')
        n.appendChild(self._doc.createTextNode(name.upper()))
        simple.appendChild(n)
        v = self._doc.createElement('String')
        v.appendChild(self._doc.createTextNode(str(val)))
        simple.appendChild(v)
        tag.appendChild(simple)
    
    def _add_date(self, tag, name, date):
        '''Translates a datetime object into a UTC timestamp and adds it to
        the specified XML tag.'''
        if date is not None:
            self._add_simple(tag, name, date.strftime('%Y-%m-%d'))
    
    def _add_tags(self, version):
        'Adds most simple metadata tags to the XML tree.'
        utc = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if self.source.get('title') is not None:
            self._add_simple(self._show, 'title', self.source.get('title'))
        if self.source.get('category') is not None:
            self._add_simple(self._show, 'genre', self.source.get('category'))
        if self.source.get('seasoncount') is not None:
            self._add_simple(self._show, 'part_number',
                             self.source.get('seasoncount'))
        if self.source.get('season') is not None:
            self._add_simple(self._season, 'part_number',
                             self.source.get('season'))
        if self.source.get('episodecount') is not None:
            self._add_simple(self._season, 'total_parts',
                             self.source.get('episodecount'))
        tags = {'subtitle' : 'subtitle', 'episode' : 'part_number',
                'syndicatedepisodenumber' : 'catalog_number',
                'channel' : 'distributed_by', 'description' : 'description',
                'rating' : 'law_rating'}
        for key, val in tags.iteritems():
            if self.source.get(key) is not None:
                self._add_simple(self._ep, val, self.source.get(key))
        if self.source.get('popularity'):
            popularity = self.source.get('popularity') / 51.0 * 2
            popularity = round(popularity) / 2.0
            self._add_simple(self._ep, 'rating', popularity)
        self._add_date(self._ep, 'date_released',
                       self.source.get('originalairdate'))
        self._add_date(self._ep, 'date_recorded', self.source.time)
        self._add_simple(self._ep, 'date_encoded', utc)
        self._add_simple(self._ep, 'date_tagged', utc)
        self._add_simple(self._ep, 'encoder', version)
        self._add_simple(self._ep, 'fps', self.source.fps)
    
    def _credits(self):
        'Adds a list of credited people into the XML tree.'
        for person in self.source.get('credits'):
            if person[1] in ['actor', 'host', 'guest_star', '']:
                self._add_simple(self._ep, 'actor', person[0])
            elif person[1] == 'director':
                self._add_simple(self._ep, 'director', person[0])
            elif person[1] == 'executive_producer':
                self._add_simple(self._ep, 'executive_producer', person[0])
            elif person[1] == 'producer':
                self._add_simple(self._ep, 'producer', person[0])
            elif person[1] == 'writer':
                self._add_simple(self._ep, 'written_by', person[0])
    
    def write(self, version):
        '''Writes the metadata XML file and returns command-line arguments to
        mkvmerge in order to embed it, along with album artwork if available,
        into the final MKV file.'''
        _clean(self._tags)
        if not self.enabled:
            return []
        logging.info('*** Adding metadata to %s ***' % self.source.mkv)
        self._add_tags(version)
        self._credits()
        data = self._doc.toprettyxml(encoding = 'UTF-8', indent = '  ')
        data = _filter_xml(data)
        logging.debug('Tag XML file:')
        logging.debug(data)
        with open(self._tags, 'w') as dest:
            dest.write(data)
        args = ['--global-tags', self._tags]
        if self.source.get('albumart') is not None:
            args += ['--attachment-description', 'Episode preview']
            args += ['--attachment-mime-type', 'image/jpeg']
            args += ['--attach-file', self.source.get('albumart')]
        return args
    
    def clean_tmp(self):
        'Removes the XML tags file if it exists.'
        _clean(self._tags)

class Transcoder:
    '''Base class which invokes tools necessary to extract, split, demux
    and encode the media.'''
    seg = 0
    video = ''
    audio = ''
    subtitles = None
    chapters = None
    metadata = None
    _split = []
    _demuxed = []
    _demux_v = None
    _demux_a = None
    _frames = 0
    _extra = 0
    
    def __init__(self, source, opts):
        self.source = source
        self.opts = opts
        self._join = source.base + '-join.ts'
        self._demux = source.base + '-demux'
        self._wav = source.base + '.wav'
        self._h264 = source.base + '.h264'
        self._vp8 = source.base + '.vp8'
        self._aac = source.base + '.aac'
        self._ogg = source.base + '.ogg'
        self._flac = source.base + '.flac'
        self.check()
    
    def check(self):
        '''Determines whether ffmpeg is installed and has built-in support
        for the requested encoding method.'''
        codecs = ['ffmpeg', '-codecs']
        if not _ffmpeg_ver():
            raise RuntimeError('FFmpeg is not installed.')
        if not self.opts.vp8 and not _ver(codecs, '--enable-(libx264)'):
            raise RuntimeError('FFmpeg does not support libx264.')
        if self.opts.vp8 and not _ver(codecs, '--enable-(libvpx)'):
            raise RuntimeError('FFmpeg does not support libvpx.')
        if self.opts.nero:
            if not _nero_ver():
                raise RuntimeError('neroAacEnc is not installed.')
        elif self.opts.flac:
            if not _ver(codecs, 'EA.*(flac)'):
                raise RuntimeError('FFmpeg does not support FLAC.')
        elif self.opts.vp8:
            if not _ver(codecs, '--enable-(libvorbis)'):
                raise RuntimeError('FFmpeg does not support libvorbis.')
        elif not _ver(codecs, '--enable-(libfaac)'):
            raise RuntimeError('FFmpeg does not support libfaac. ' +
                               '(Perhaps try using neroAacEnc?)')
        if not self.source.meta_present:
            self.metadata.enabled = False
    
    def _extract(self, clip, elapsed):
        '''Creates a new chapter marker at elapsed and uses ffmpeg to extract
        an MPEG-TS video clip from clip[0] to clip[1]. All values are
        in seconds.'''
        logging.info('*** Extracting segment %d: [%s - %s] ***' %
                     (self.seg, _seconds_to_time(clip[0]),
                      _seconds_to_time(clip[1])))
        self.chapters.add(elapsed, self.seg)
        args = ['ffmpeg', '-y', '-i', self.source.orig, '-ss', str(clip[0]),
                '-t', str(clip[1] - clip[0])] + self.source.split_args[0]
        split = '%s-%d.ts' % (self.source.base, self.seg)
        args += [split]
        self._split += split
        if len(self.source.split_args) > 1:
            args += self.source.split_args[1]
        _cmd(args)
        self.seg += 1
        self.subtitles.mark(clip[1])
    
    def split(self):
        '''Uses the source's cutlist to mark specific video clips from the
        source video for extraction while also setting chapter markers.'''
        (pos, elapsed) = (0, 0)
        for cut in self.source.cutlist:
            (start, end) = cut
            if start > self.opts.thresh and start > pos:
                self._extract((pos, start), elapsed)
            elapsed += start - pos
            pos = end
        if pos < self.source.duration - self.opts.thresh:
            self._extract((pos, self.source.duration), elapsed)
            elapsed += self.source.duration - pos
            pos = self.source.duration
        self.chapters.add(elapsed, None)
    
    def join(self):
        '''Uses ffmpeg's concat: protocol to rejoin the previously split
        video clips, and then extracts subtitles from the resulting video.'''
        logging.info('*** Joining video to %s ***' % self._join)
        self.subtitles.clean_tmp()
        concat = 'concat:'
        for seg in xrange(0, self.seg):
            name = '%s-%d.ts' % (self.source.base, seg)
            if os.path.exists(name):
                concat += '%s|' % name
            else:
                raise RuntimeError('Could not find video segment %s.' % name)
        concat = concat[:-1]
        args = ['ffmpeg', '-y', '-i', concat] + self.source.split_args[0]
        args += [self._join]
        if len(self.source.split_args) > 1:
            args += self.source.split_args[1]
        _cmd(args)
        self.subtitles.extract(self._join)
        self.subtitles.adjust()
    
    def _find_streams(self):
        '''Locates the PID numbers of the video and audio streams to be encoded
        using ffmpeg.'''
        (vstreams, astreams) = ([], [])
        stream = 'Stream.*\[0x([0-9a-fA-F]+)\].*'
        videoRE = re.compile(stream + 'Video:.*\s+([0-9]+)x([0-9]+)')
        audioRE = re.compile(stream + ':\s*Audio')
        proc = subprocess.Popen(['ffmpeg', '-i', self._join],
                                stdout = subprocess.PIPE,
                                stderr = subprocess.STDOUT)
        for line in proc.stdout:
            match = re.search(videoRE, line)
            if match:
                enabled = False
                if re.search('Video:.*\(Main\)', line):
                    logging.debug('Found video stream 0x%s' %
                                  match.group(1))
                    enabled = True
                vstreams += [(int(match.group(1), 16), enabled)]
            match = re.search(audioRE, line)
            if match:
                enabled = False
                pid = int(match.group(1), 16)
                match = re.search('\]\(([A-Za-z]+)\)', line)
                if match:
                    if match.group(1) == _iso_639_2(self.opts.language):
                        logging.debug('Found audio stream 0x%s' %
                                      match.group(1))
                        enabled = True
                astreams += [(pid, enabled)]
            proc.wait()
        if len(vstreams) == 0:
            raise RuntimeError('No video streams could be found.')
        if len(astreams) == 0:
            raise RuntimeError('No audio streams could be found.')
        if len([vid[0] for vid in vstreams if vid[1]]) == 0:
            logging.debug('No detected video streams, enabling %s' %
                          hex(vstreams[0][0]))
            vstreams[0] = (vstreams[0][0], True)
        if len([aud[0] for aud in astreams if aud[1]]) == 0:
            logging.debug('No detected audio streams, enabling %s' %
                          hex(astreams[0][0]))
            astreams[0] = (astreams[0][0], True)
        return vstreams, astreams
    
    def _find_demux(self):
        'Uses the Project-X log to obtain the separated video / audio files.'
        (vstreams, astreams) = self._find_streams()
        with open('%s_log.txt' % self._demux, 'r') as log:
            videoRE = re.compile('Video: PID 0x([0-9A-Fa-f]+)')
            audioRE = re.compile('Audio: PID 0x([0-9A-Fa-f]+)')
            fileRE = re.compile('\'(.*)\'\s*$')
            (curr_v, curr_a) = (1, 1)
            (found_v, found_a) = (0, 0)
            (targ_v, targ_a) = (0, 0)
            for line in log:
                match = re.search(videoRE, line)
                if match:
                    found_v += 1
                    pid = int(match.group(1), 16)
                    for vid in vstreams:
                        if vid[0] == pid and vid[1]:
                            targ_v = found_v
                match = re.search(audioRE, line)
                if match:
                    found_a += 1
                    pid = int(match.group(1), 16)
                    for aud in astreams:
                        if aud[0] == pid and aud[1]:
                            targ_a = found_a
                if re.match('\.Video ', line):
                    match = re.search(fileRE, line)
                    if match:
                        self._demuxed += match.group(1)
                        if curr_v == targ_v:
                            self._demux_v = match.group(1)
                    curr_v += 1
                elif re.match('Audio \d', line):
                    match = re.search(fileRE, line)
                    if match:
                        self._demuxed += match.group(1)
                        if curr_a == targ_a:
                            self._demux_a = match.group(1)
                    curr_a += 1
    
    def demux(self):
        '''Invokes Project-X to clean noise from raw MPEG-2 video capture data,
        split the media along previously specified cutpoints and combine into
        one file, and then extract each media stream (video / audio) from the
        container into separate raw data files.'''
        logging.info('*** Demuxing video ***')
        name = os.path.split(self._demux)[-1]
        try:
            _cmd(['java', '-jar', self.opts.projectx, '-out', self.opts.tmp,
                  '-name', name, '-demux', self._join])
        except RuntimeError:
            raise RuntimeError('Could not demux video.')
        self._find_demux()
        if self._demux_v is None or not os.path.exists(self._demux_v):
            raise RuntimeError('Could not locate demuxed video stream.')
        if self._demux_a is None or not os.path.exists(self._demux_a):
            raise RuntimeError('Could not locate demuxed audio stream.')
        logging.debug('Demuxed video: %s' % self._demux_v)
        logging.debug('Demuxed audio: %s' % self._demux_a)
    
    def _adjust_res(self):
        '''Adjusts the resolution of the transcoded video to be exactly the
        desired target resolution (if one is chosen), preserving aspect ratio
        by padding extra space with black pixels.'''
        size = []
        res = self.source.resolution
        target = self.opts.resolution
        if target is not None:
            aspect = res[0] * 1.0 / res[1]
            if aspect > target[0] * 1.0 / target[1]:
                vres = int(round(target[0] / aspect))
                if vres % 2 == 1:
                    vres += 1
                pad = (target[1] - vres) / 2
                size = ['-s', '%dx%d' % (target[0], vres), '-vf',
                        'pad=%d:%d:0:%d:black' % (target[0], target[1], pad)]
            else:
                hres = int(round(target[1] * aspect))
                if hres % 2 == 1:
                    hres += 1
                pad = (target[0] - hres) / 2
                size = ['-s', '%dx%d' % (hres, target[1]), '-vf',
                        'pad=%d:%d:%d:0:black' % (target[0], target[1], pad)]
        return size
    
    def encode_video(self):
        'Invokes ffmpeg to transcode the video stream to H.264 or VP8.'
        prof = []
        (fmt, codec, target) = ('', '', '')
        if self.opts.preset:
            prof = ['-vpre', self.opts.preset]
        if self.opts.ipod:
            (fmt, codec, target) = ('ipod', 'libx264', self._h264)
        elif self.opts.webm:
            (fmt, codec, target) = ('webm', 'libvpx', self._vp8)
        elif self.opts.vp8:
            (fmt, codec, target) = ('matroska', 'libvpx', self._vp8)
        else:
            (fmt, codec, target) = ('mp4', 'libx264', self._h264)
        _clean(target)
        size = self._adjust_res()
        common = ['ffmpeg', '-y', '-i', self._demux_v, '-vcodec', codec,
                  '-an', '-threads', 0, '-f', fmt] + prof + size
        if self.opts.two_pass:
            logging.info(u'*** Encoding video to %s - first pass ***' %
                         self.opts.tmp)
            common += ['-vb', '%dk' % self.opts.video_br]
            _cmd(common + ['-pass', 1, os.devnull],
                 cwd = self.opts.tmp)
            logging.info(u'*** Encoding video to %s - second pass ***' %
                         target)
            _cmd(common + ['-pass', 2, target],
                 cwd = self.opts.tmp)
        else:
            logging.info(u'*** Encoding video to %s ***' % target)
            _cmd(common + ['-crf', self.opts.video_crf, target])
        self.video = target
    
    def encode_audio(self):
        '''Invokes ffmpeg or neroAacEnc to transcode the audio stream to
        AAC, Vorbis or FLAC.'''
        (fmt, codec, target) = ('', '', '')
        if self.opts.flac:
            (fmt, codec, target) = ('flac', 'flac', self._flac)
        elif self.opts.vp8:
            (fmt, codec, target) = ('ogg', 'libvorbis', self._ogg)
        else:
            (fmt, codec, target) = ('aac', 'libfaac', self._aac)
        _clean(target)
        logging.info(u'*** Encoding audio to %s ***' % target)
        if fmt == 'aac' and self.opts.nero:
            _cmd(['ffmpeg', '-y', '-i', self._demux_a, '-vn', '-acodec',
                  'pcm_s16le', '-f', 'wav', self._wav])
            _cmd(['neroAacEnc', '-q', self.opts.audio_q, '-if', self._wav,
                  '-of', target])
        else:
            _cmd(['ffmpeg', '-y', '-i', self._demux_a, '-vn', '-acodec',
                  codec, '-ab', '%dk' % self.opts.audio_br, '-f', fmt,
                  target])
        self.audio = target
    
    def clean_video(self):
        'Removes the temporary video stream data.'
        _clean(self._demux_v)
    
    def clean_audio(self):
        'Removes the temporary audio stream data.'
        _clean(self._demux_a)
    
    def clean_tmp(self):
        'Removes any temporary files generated during encoding.'
        self.clean_video()
        self.clean_audio()
        for split in self._split:
            _clean(split)
        for demux in self._demuxed:
            _clean(demux)
        _clean(self._join)
        _clean(self._wav)
        _clean(self.video)
        _clean(self.audio)
        for log in ['ffmpeg2pass-0.log', 'x264_2pass.log',
                    'x264_2pass.log.mbtree', '%s_log.txt' % self._demux,
                    '%s_log.txt' % self.source.base]:
            _clean(os.path.join(self.opts.tmp, log))

class MP4Transcoder(Transcoder):
    'Remuxes and finalizes MPEG-4 media files.'
    
    def __init__(self, source, opts):
        Transcoder.__init__(self, source, opts)
        self.subtitles = MP4Subtitles(source)
        self.chapters = MP4Chapters(source)
        self.metadata = MP4Metadata(source)
        self.check()
    
    def check(self):
        'Determines whether MP4Box is installed.'
        if not _ver(['MP4Box', '-version'], 'version\s*([0-9.DEV]+)'):
            raise RuntimeError('MP4Box is not installed.')
        if not self.source.meta_present:
            self.metadata.enabled = False
    
    def remux(self):
        '''Invokes MP4Box to combine the audio, video and subtitle streams
        into the MPEG-4 target file, also embedding chapter data and
        metadata.'''
        logging.info(u'*** Remuxing to %s ***' % self.source.mp4)
        self.source.make_final_dir()
        _clean(self.source.mp4)
        common = ['MP4Box', '-tmp', self.opts.final_path]
        _cmd(common + ['-new', '-add', '%s#video:name=Video' % self.video,
                       '-add', '%s#audio:name=Audio' % self.audio,
                       self.source.mp4])
        _cmd(common + ['-isma', '-hint', self.source.mp4])
        self.subtitles.write()
        self.chapters.write()
        _cmd(common + ['-lang', self.opts.language, self.source.mp4])
        self.metadata.write(_version(self.opts))
    
    def clean_tmp(self):
        'Removes any temporary files generated during encoding.'
        Transcoder.clean_tmp(self)
        self.subtitles.clean_tmp()
        self.chapters.clean_tmp()
        self.metadata.clean_tmp()

class MKVTranscoder(Transcoder):
    'Remuxes and finalizes Matroska media files.'
    
    def __init__(self, source, opts):
        Transcoder.__init__(self, source, opts)
        self.subtitles = MKVSubtitles(source)
        self.chapters = MKVChapters(source)
        self.metadata = MKVMetadata(source)
        self.check()
    
    def check(self):
        'Determines whether mkvmerge is installed.'
        if not _ver(['mkvmerge', '--version'], '\sv*([0-9.]+)'):
            raise RuntimeError('mkvmerge is not installed.')
        if not self.source.meta_present:
            self.metadata.enabled = False
    
    def remux(self):
        '''Invokes mkvmerge to combine the audio, video and subtitle streams
        into the Matroska target file, also embedding chapter data and
        metadata.'''
        logging.info(u'*** Remuxing to %s ***' % self.source.mkv)
        self.source.make_final_dir()
        _clean(self.source.mkv)
        common = ['--no-chapters', '-B', '-T', '-M', '--no-global-tags']
        args = ['mkvmerge']
        args += ['--default-language', _iso_639_2(self.opts.language)]
        args += common + ['-A', '-S', '--track-name', '0:Video', self.video]
        args += common + ['-D', '-S', '--track-name', '0:Audio', self.audio]
        subs = self.subtitles.write()
        if len(subs) > 0:
            args += common + ['-A', '-D'] + subs
        args += self.chapters.write()
        args += self.metadata.write(_version(self.opts))
        if self.opts.webm:
            args += ['--webm']
        args += ['-o', self.source.mkv]
        _cmd(args)
    
    def clean_tmp(self):
        'Removes any temporary files generated during encoding.'
        Transcoder.clean_tmp(self)
        self.subtitles.clean_tmp()
        self.chapters.clean_tmp()
        self.metadata.clean_tmp()

class Source(dict):
    '''Acts as a base class for various raw video sources and handles
    Tvdb metadata.'''
    fps = None
    resolution = None
    duration = None
    cutlist = None
    vstreams = 0
    astreams = 0
    base = None
    orig = None
    rating = None
    final = None
    mp4 = None
    mkv = None
    meta_present = False
    split_args = None
    
    def __repr__(self):
        season = int(self.get('season', 0))
        episode = int(self.get('episode', 0))
        prodcode = self.get('syndicatedepisodenumber', '(?)')
        show = self.get('title')
        name = self.get('subtitle')
        s = ''
        if show and name:
            if season > 0 and episode > 0:
                s = u'%s %02dx%02d - %s' % (show, season, episode, name)
            else:
                s = u'%s %s - %s' % (show, prodcode, name)
        else:
            s = self.base
        return u'<Source \'%s\' at %s>' % (s, hex(id(self)))
    
    def __init__(self, opts):
        self.opts = opts
        if opts.tmp:
            self.remove_tmp = False
        else:
            opts.tmp = tempfile.mkdtemp(prefix = u'transcode_')
            self.remove_tmp = True
        if opts.ipod:
            self.mp4ext = 'm4v'
        else:
            self.mp4ext = 'mp4'
        self.tvdb = MythTV.ttvdb.tvdb_api.Tvdb(language = self.opts.language)
    
    def _check_split_args(self):
        '''Determines the arguments to pass to FFmpeg when copying video data
        during splits or frame queries. Versions 0.7 and older use -newaudio /
        -newvideo tags for each additional stream present. Versions 0.8 and
        newer use -map 0:v -map 0:a to automatically copy over all streams.'''
        match = re.search('[Ff]+mpeg\s+(.*)$', _ffmpeg_ver())
        if not match:
            raise RuntimeError('FFmpeg version could not be determined.')
        ver = match.group(1)
        match = re.match('^([0-9]+\.[0-9]+)', ver)
        if match and float(match.group(1)) <= 0.7:
            args = [['-acodec', 'copy', '-vcodec', 'copy',
                     '-f', 'mpegts']]
            for astream in xrange(1, self.astreams):
                args += [['-acodec', 'copy', '-newaudio']]
            for vstream in xrange(1, self.vstreams):
                args += [['-vcodec', 'copy', '-newvideo']]
        else:
            args = [['-map', '0:v', '-map', '0:a', '-c', 'copy',
                     '-f', 'mpegts']]
        self.split_args = args
    
    def video_params(self):
        '''Obtains source media parameters such as resolution and FPS
        using ffmpeg.'''
        (fps, resolution, duration) = (None, None, None)
        (vstreams, astreams) = (0, 0)
        stream = 'Stream.*\[0x[0-9a-fA-F]+\].*'
        fpsRE = re.compile('([0-9]*\.?[0-9]*) tbr')
        videoRE = re.compile(stream + 'Video:.*\s+([0-9]+)x([0-9]+)')
        audioRE = re.compile(stream + ':\s*Audio')
        duraRE = re.compile('Duration: ([0-9]+):([0-9]+):([0-9]+)\.([0-9]+)')
        try:
            proc = subprocess.Popen(['ffmpeg', '-i', self.orig],
                                    stdout = subprocess.PIPE,
                                    stderr = subprocess.STDOUT)
            for line in proc.stdout:
                match = re.search(fpsRE, line)
                if match:
                    fps = float(match.group(1))
                match = re.search(videoRE, line)
                if match:
                    vstreams += 1
                    if resolution is None:
                        width = int(match.group(1))
                        height = int(match.group(2))
                        resolution = (width, height)
                match = re.search(audioRE, line)
                if match:
                    astreams += 1
                match = re.search(duraRE, line)
                if match:
                    hour = int(match.group(1))
                    minute = int(match.group(2))
                    sec = int(match.group(3))
                    frac = int(match.group(4))
                    duration = hour * 3600 + minute * 60 + sec
                    duration += frac / (10. ** len(match.group(4)))
            proc.wait()
        except OSError:
            raise RuntimeError('FFmpeg is not installed.')
        if vstreams == 0:
            raise RuntimeError('No video streams could be found.')
        if astreams == 0:
            raise RuntimeError('No audio streams could be found.')
        self._check_split_args()
        return fps, resolution, duration, vstreams, astreams
    
    def parse_resolution(self, res):
        '''Translates the user-specified target resolution string into a
        width/height tuple using predefined resolution names like '1080p',
        or aspect ratios like '4:3', and so on.'''
        if res:
            predefined = {'480p' : (640, 480), '480p60' : (720, 480),
                          '720p' : (1280, 720), '1080p' : (1920, 1080)}
            for key, val in predefined.iteritems():
                if res == key:
                    return val
            match = re.match('(\d+)x(\d+)', res)
            if match:
                return (int(match.group(1)), int(match.group(2)))
            match = re.match('(\d+):(\d+)', res)
            if match:
                aspect = float(match.group(1)) / float(match.group(2))
                h = self.resolution[1]
                w = int(round(h * aspect))
                return (w, h)
            if res == 'close' or res == 'closest':
                (w, h) = self.resolution
                aspect = w * 1.0 / h
                closest = (-1920, -1080)
                for val in predefined.itervalues():
                    test = math.sqrt((val[0] - w) ** 2 + (val[1] - h) ** 2)
                    curr = math.sqrt((closest[0] - w) ** 2 +
                                     (closest[1] - h) ** 2)
                    if test < curr:
                        closest = val
                return closest
            logging.warning('*** Invalid resolution - %s ***' % res)
        return None
    
    def _align_episode(self):
        '''Returns a string for the episode number padded by as many zeroes
        needed so that the filename for the last episode in the season will
        align perfectly with this filename. Usually a field width of two,
        since most seasons tend to have less than 100 episodes.'''
        ep = self.get('episode')
        if ep is None:
            ep = ''
        if self.get('episodecount') is None:
            return ep
        lg = math.log(self['episodecount']) / math.log(10)
        field = int(math.ceil(lg))
        return ('%0' + str(field) + 'd') % ep
    
    def _find_episode(self, show):
        'Searches Tvdb for the episode using the original air date and title.'
        episodes = None
        airdate = self.get('originalairdate')
        subtitle = self.get('subtitle')
        if airdate is not None:
            episodes = show.search(airdate, key = 'firstaired')
        if not episodes and subtitle is not None:
            episodes = show.search(subtitle, key = 'episodename')
        if len(episodes) == 1:
            return episodes[0]
        for ep in episodes:
            air = ep.get('firstaired')
            if air:
                date = datetime.datetime.strptime(air, '%Y-%m-%d').date()
                if airdate == date:
                    return ep
        for ep in episodes:
            st = ep.get('episodename')
            if subtitle.find(st) >= 0 or st.find(subtitle) >= 0:
                return ep
        return None
    
    def fetch_tvdb(self):
        'Obtains missing metadata through Tvdb if episode is found.'
        if not self.get('title'):
            return
        try:
            show = self.tvdb[self.get('title')]
            self['seasoncount'] = len(show)
            if show.has_key(0):
                self['seasoncount'] -= 1
            ep = self._find_episode(show)
            logging.debug('Tvdb episode: %s' % ep)
            if ep:
                if self.get('subtitle') is None:
                    self['subtitle'] = ep.get('episodename')
                if ep.get('episodenumber') is not None:
                    self['episode'] = int(ep.get('episodenumber'))
                elif ep.get('combined_episodenumber') is not None:
                    self['episode'] = int(ep.get('combined_episodenumber'))
                season = ep.get('seasonnumber')
                if season is None:
                    season = int(ep.get('combined_season'))
                if season is not None:
                    self['episodecount'] = len(show[int(season)])
                    self['season'] = int(season)
                if self['season'] is not None and self['episode'] is not None:
                    if self.get('syndicatedepisodenumber') is None:
                        self['syndicatedepisodenumber'] = '%d%s' % \
                            (self['season'], self._align_episode())
                overview = ep.get('overview')
                if self.get('description') is None:
                    self['description'] = overview
                elif overview is not None:
                    if self.opts.use_tvdb_descriptions:
                        if len(self['description']) < len(overview):
                            self['description'] = overview
                rating = ep.get('rating')
                if rating is not None:
                    self['popularity'] = int(float(rating) / 10 * 255)
                    if self.opts.use_tvdb_rating:
                        self['description'] += ' (%s / 10)' % rating
                filename = ep.get('filename')
                if filename is not None and len(filename) > 0:
                    ext = os.path.splitext(filename)[-1]
                    art = self.base + ext
                    try:
                        urllib.urlretrieve(filename, art)
                        self['albumart'] = art
                    except IOError:
                        logging.warning('*** Unable to download ' +
                                        'episode screenshot ***')
        except MythTV.ttvdb.tvdb_exceptions.tvdb_shownotfound:
            logging.warning('*** Unable to fetch Tvdb listings for show ***')
    
    def sort_credits(self):
        '''Sorts the list of credited actors, directors and such using the
        last name first.'''
        if self.get('credits'):
            key = lambda person: u'%s %s' % (person[1],
                                             _last_name_first(person[0]))
            self['credits'] = sorted(self.get('credits'), key = key)
    
    def print_metadata(self):
        'Outputs any metadata obtained to the log.'
        if not self.meta_present:
            return
        logging.info('*** Printing file metadata ***')
        for key in self.keys():
            if key != 'credits':
                logging.info(u'  %s: %s' % (key, self[key]))
        cred = self.get('credits')
        if cred:
            logging.info('  Credits:')
            for person in cred:
                name = person[0]
                role = re.sub('_', ' ', person[1]).lower()
                logging.info(u'    %s (%s)' % (name, role))
        else:
            logging.info('  No credits')
    
    def print_options(self):
        'Outputs the user-selected options to the log.'
        logging.info('*** Printing configuration ***')
        (fmt, audio, video, ext) = '', '', '', ''
        if self.opts.webm:
            (fmt, ext) = 'WebM', 'webm'
        elif self.opts.ipod:
            (fmt, ext) = 'iPod Touch compatible MPEG-4', 'm4v'
        elif self.opts.matroska:
            (fmt, ext) = 'Matroska', 'mkv'
        else:
            (fmt, ext) = 'MPEG-4', 'mp4'
        if self.opts.vp8:
            video = 'On2 VP8'
        else:
            video = 'H.264 AVC'
        if self.opts.flac:
            audio = 'FLAC'
        elif self.opts.vp8:
            audio = 'Ogg Vorbis'
        elif self.opts.nero:
            audio = 'AAC (neroAacEnc)'
        else:
            audio = 'AAC (libfaac)'
        logging.info('  Format: %s, %s, %s' % (fmt, video, audio))
        enc = '  Video options:'
        if self.opts.preset:
            enc += ' preset \'%s\',' % self.opts.preset
        if self.opts.resolution:
            enc += ' resolution %dx%d,' % self.opts.resolution
        if self.opts.two_pass:
            enc += ' two-pass, br: %dk' % self.opts.video_br
        else:
            enc += ' one-pass, crf: %d' % self.opts.video_crf
        logging.info(enc)
        logging.info('  Source file: %s' % self.orig)
        logging.info('  Target file: %s.%s' % (self.final, ext))
        logging.info('  Temporary directory: %s' % self.opts.tmp)
    
    def final_name(self):
        '''Obtains the filename of the target MPEG-4 file using the format
        string. Formatting is (mostly) compatible with Recorded.formatPath()
        from dataheap.py, and the format used in mythrename.pl.'''
        final = os.path.join(self.opts.final_path,
                             os.path.split(self.base)[-1])
        if self.meta_present:
            path = self.opts.format
            tags = [('%T', 'title'), ('%S', 'subtitle'), ('%R', 'description'),
                    ('%C', 'category'), ('%n', 'syndicatedepisodenumber'),
                    ('%s', 'season'), ('%E', 'episode'), ('%r', 'rating')]
            for tag, key in tags:
                if self.get(key) is not None:
                    val = unicode(self.get(key))
                    val = val.replace('/', self.opts.replace_char)
                    if os.name == 'nt':
                        val = val.replace('\\', self.opts.replace_char)
                    path = path.replace(tag, val)
                else:
                    path = path.replace(tag, '')
            if self.get('episode') is not None:
                path = path.replace('%e', self._align_episode())
            else:
                path = path.replace('%e', '')
            tags = [('%oy', '%y'), ('%oY', '%Y'), ('%on', '%m'),
                    ('%om', '%m'), ('%oj', '%d'), ('%od', '%d')]
            airdate = self.get('originalairdate')
            for tag, part in tags:
                if airdate is not None:
                    val = unicode(airdate.strftime(part))
                    path = path.replace(tag, val)
                else:
                    path = path.replace(tag, '')
            path = path.replace('%-', '-')
            path = path.replace('%%', '%')
            path = _sanitize(path, self.opts.replace_char)
            final = os.path.join(self.opts.final_path, *path.split('/'))
        return final
    
    def make_final_dir(self):
        'Creates the directory for the target MPEG-4 file.'
        path = os.path.dirname(self.final)
        if not os.path.isdir(path):
            os.makedirs(path, 0755)
    
    def clean_copy(self):
        'Removes the copied raw MPEG-2 video data.'
        _clean(self.orig)
    
    def clean_tmp(self):
        'Removes any temporary files created.'
        self.clean_copy()
        art = self.get('albumart')
        if art:
            _clean(art)
        if self.remove_tmp and os.path.isdir(self.opts.tmp):
            shutil.rmtree(self.opts.tmp)

class MythSource(Source):
    '''Obtains the raw MPEG-2 video data from a MythTV database along with
    metadata and a commercial-skip cutlist.'''
    prog = None
    
    class _Rating(MythTV.DBDataRef):
        'Query for the content rating within the MythTV database.'
        _table = 'recordedrating'
        _ref = ['chanid', 'starttime']
    
    def __init__(self, channel, time, opts):
        Source.__init__(self, opts)
        self.channel = channel
        self.time = _convert_time(time)
        self.base = os.path.join(opts.tmp, '%s_%s' % (str(channel), str(time)))
        self.orig = self.base + '-orig.mpg'
        self.db_info = {'DBHostName' : opts.host, 'DBName' : opts.database,
                        'DBUserName' : opts.user, 'DBPassword' : opts.password,
                        'SecurityPin' : opts.pin}
        self.db = MythTV.MythDB(**self.db_info)
        try:
            self.rec = MythTV.Recorded((channel, time), db = self.db)
        except MythTV.exceptions.MythError:
            err = 'Could not find recording at channel %d and time %s.'
            raise ValueError(err % (channel, time))
        try:
            self.prog = self.rec.getRecordedProgram()
            self.meta_present = True
        except MythTV.exceptions.MythError:
            logging.warning('*** No MythTV program data, ' +
                            'metadata disabled ***')
        self.rating = self._Rating(self.rec._wheredat, self.db)
        self._fetch_metadata()
        self.final = self.final_name()
        self.mp4 = '%s.%s' % (self.final, self.mp4ext)
        if self.opts.webm:
            self.mkv = self.final + '.webm'
        else:
            self.mkv = self.final + '.mkv'
    
    def _frame_to_timecode(self, frame):
        '''Uses ffmpeg to remux a given number of frames in the video file
        in order to determine the amount of time elapsed by those frames.'''
        time = 0
        args = ['ffmpeg', '-y', '-i', self.orig, '-vframes',
                str(frame)] + self.split_args[0]
        args += [os.devnull]
        if len(self.split_args) > 1:
            args += self.split_args[1]
        proc = subprocess.Popen(args, stdout = subprocess.PIPE,
                                stderr = subprocess.STDOUT)
        logging.debug('$ %s' % u' '.join(args))
        regex = re.compile('time=(\d\d):(\d\d):(\d\d\.\d\d)(?!.*time)')
        for line in proc.stdout:
            match = re.search(regex, line)
            if match:
                time = 3600 * int(match.group(1)) + 60 * int(match.group(2))
                time += float(match.group(3))
        return time
    
    def _cut_list(self):
        'Obtains the MythTV commercial-skip cutlist from the database.'
        logging.info('*** Locating cut points ***')
        markup = self.rec.markup.getcutlist()
        self.cutlist = []
        for cut in xrange(0, len(markup)):
            start = self._frame_to_timecode(markup[cut][0])
            end = self._frame_to_timecode(markup[cut][1])
            self.cutlist.append((start, end))
    
    def _fetch_metadata(self):
        'Obtains any metadata MythTV might have stored in the database.'
        if not self.meta_present:
            return
        logging.info('*** Fetching metadata for %s ***' % self.base)
        channel = None
        ch = MythTV.Channel(db = self.db)
        for chan in ch._fromQuery("", (), db = self.db):
            if chan.get('chanid') == self.rec.get('chanid'):
                channel = chan
        if channel:
            self['channel'] = channel.get('name')
        for key in ['title', 'subtitle', 'description', 'category',
                    'originalairdate', 'syndicatedepisodenumber']:
            val = self.prog.get(key)
            if val is not None:
                self[key] = val
        if self.rec.get('originalairdate') is None: # MythTV bug
            self.rec['originalairdate'] = self.time
        for item in self.rating:
            self['rating'] = item.get('rating')
        cred = None
        for person in self.rec.cast:
            if cred is None:
                cred = []
            cred.append((person['name'], person['role']))
        self['credits'] = cred
        self.sort_credits()
        self.fetch_tvdb()
    
    def copy(self):
        '''Copies the recording for the given channel ID and start time to the
        specified path, if enough space is available.'''
        bs = 4096 * 1024
        logging.info('*** Copying video to %s ***' % self.orig)
        source = self.rec.open()
        try:
            with open(self.orig, 'wb') as dest:
                data = source.read(bs);
                while len(data) > 0:
                    dest.write(data)
                    data = source.read(bs)
        finally:
            source.close()
        (self.fps, self.resolution, self.duration,
         self.vstreams, self.astreams) = self.video_params()
        if not self.fps or not self.resolution or not self.duration:
            raise RuntimeError('Could not determine video parameters.')
        self.opts.resolution = self.parse_resolution(self.opts.resolution)
        self._cut_list()

class WTVSource(Source):
    '''Obtains the raw MPEG-2 video data from a Windows TV recording (.WTV)
    along with embedded metadata.'''
    channel = None
    time = None
    orig = None
    
    def __init__(self, wtv, opts):
        Source.__init__(self, opts)
        self.wtv = wtv
        match = re.search('[^_]*_([^_]*)_(\d\d\d\d)_(\d\d)_(\d\d)_' +
                          '(\d\d)_(\d\d)_(\d\d)\.[Ww][Tt][Vv]', wtv)
        if match:
            self.channel = match.group(1)
            t = ''
            for g in xrange(2, 8):
                t += match.group(g)
            self.time = _convert_time(long(t))
            b = self.channel + '_' + t
            self.base = os.path.join(opts.tmp, '%s_%s' % (self.channel, t))
        else:
            f = os.path.split(wtv)[-1]
            self.base = os.path.join(opts.tmp, os.path.splitext(f)[0])
        self.orig = self.base + '-orig.ts'
        self._fetch_metadata()
        self.final = self.final_name()
        self.mp4 = '%s.%s' % (self.final, self.mp4ext)
        self.mkv = self.final + '.mkv'
    
    def _cut_list(self):
        '''Obtains a commercial-skip cutlist from previously generated
        Comskip output, if it exists.'''
        self.cutlist = []
        base = os.path.splitext(self.wtv)[0]
        comskip = base + '.txt'
        if os.path.exists(comskip):
            framesRE = re.compile('^(\d+)\s+(\d+)\s*$')
            with open(comskip, 'r') as text:
                for line in text:
                    match = re.search(framesRE, line)
                    if match:
                        start = int(match.group(1)) / self.fps
                        end = int(match.group(2)) / self.fps
                        self.cutlist.append((start, end))
    
    def _parse_genre(self, genre):
        'Translates the WTV genre line into a category tag.'
        self['category'] = genre.split(';')[0]
    
    def _parse_airdate(self, airdate):
        'Translates the UTC timecode for original airdate into a date object.'
        try:
            d = datetime.datetime.strptime(airdate, '%Y-%m-%dT%H:%M:%SZ')
            self['originalairdate'] = d.date()
        except ValueError:
            pass
    
    def _parse_credits(self, line):
        'Translates the WTV credits line into a list of credited people.'
        cred = None
        people = line.split(';')
        for tup in zip(range(0, 4), ['actor', 'director',
                                     'host', 'guest_star']):
            for person in people[tup[0]].split('/'):
                if person != '':
                    if cred is None:
                        cred = []
                    cred.append((person, tup[1]))
        self['credits'] = cred
    
    def _fetch_metadata(self):
        'Obtains any metadata which might be embedded in the WTV.'
        logging.info('*** Fetching metadata for %s ***' % self.wtv)
        val = '\s*:\s*(.*)$'
        tags = []
        tags.append((re.compile('service_provider' + val), 'channel'))
        tags.append((re.compile('service_name' + val), 'channel'))
        tags.append((re.compile('\s+Title' + val), 'title'))
        tags.append((re.compile('WM/SubTitle' + val), 'subtitle'))
        tags.append((re.compile('WM/SubTitleDescription' + val),
                     'description'))
        tags.append((re.compile('genre' + val), self._parse_genre))
        tags.append((re.compile('WM/MediaOriginalBroadcastDateTime' + val),
                     self._parse_airdate))
        tags.append((re.compile('WM/ParentalRating' + val), 'rating'))
        tags.append((re.compile('WM/MediaCredits' + val), self._parse_credits))
        try:
            proc = subprocess.Popen(['ffmpeg', '-i', self.wtv],
                                    stdout = subprocess.PIPE,
                                    stderr = subprocess.STDOUT)
            for line in proc.stdout:
                for tag in tags:
                    match = re.search(tag[0], line)
                    if match:
                        self.meta_present = True
                        if type(tag[1]) == type(str()):
                            self[tag[1]] = match.group(1).strip()
                        else:
                            tag[1](match.group(1).strip())
            proc.wait()
        except OSError:
            raise RuntimeError('FFmpeg is not installed.')
        self.sort_credits()
        self.fetch_tvdb()
    
    def copy(self):
        'Extracts the MPEG-2 data from the WTV file.'
        logging.info('*** Extracting data to %s ***' % self.orig)
        try:
            _cmd(['java', '-cp', self.opts.remuxtool, 'util.WtvToMpeg',
                  '-i', self.wtv, '-o', self.orig, '-lang',
                  _iso_639_2(self.opts.language)])
        except RuntimeError:
            raise RuntimeError('Could not extract video.')
        self._fetch_metadata()
        (self.fps, self.resolution, self.duration,
         self.vstreams, self.astreams) = self.video_params()
        if not self.fps or not self.resolution or not self.duration:
            raise RuntimeError('Could not determine video parameters.')
        self.opts.resolution = self.parse_resolution(self.opts.resolution)
        self._cut_list()

if __name__ == '__main__':
    sys.stdout = codecs.getwriter('utf8')(sys.stdout)
    parser = _get_options()
    (opts, args) = parser.parse_args()
    _check_args(args, parser, opts)
    s = None
    if len(args) == 1:
        wtv = args[0]
        s = WTVSource(wtv, opts)
    else:
        channel = int(args[0])
        timecode = long(args[1])
        s = MythSource(channel, timecode, opts)
    s.copy()
    s.print_metadata()
    s.print_options()
    if opts.matroska:
        t = MKVTranscoder(s, opts)
    else:
        t = MP4Transcoder(s, opts)
    t.split()
    t.join()
    t.demux()
    s.clean_copy()
    t.encode_audio()
    t.clean_audio()
    t.encode_video()
    t.clean_video()
    t.remux()
    t.clean_tmp()
    s.clean_tmp()

# Copyright (c) 2012, Lucas Jacobs
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met: 
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer. 
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution. 
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official
# policies, either expressed or implied, of the copyright holder.

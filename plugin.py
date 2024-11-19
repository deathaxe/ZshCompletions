from __future__ import annotations
import os
import re
import subprocess

import sublime
import sublime_plugin

ZSH_CAPTURE_COMPLETION = R"""() {
zmodload zsh/zpty || { echo 'error: missing module zsh/zpty' >&2; exit 1 }

# spawn shell
zpty z zsh -f -i

# line buffer for pty output
local line

setopt rcquotes
() {
    zpty -w z source $1
    repeat 4; do
        zpty -r z line
        [[ $line == ok* ]] && return
    done
    echo 'error initializing.' >&2
    exit 2
} =( <<< '
# no prompt!
PROMPT=

# load completion system
autoload compinit
compinit -d ~/.zcompdump_capture

# never run a command
bindkey ''^M'' undefined
bindkey ''^J'' undefined
bindkey ''^I'' complete-word

# send a line with null-byte at the end before and after completions are output
null-line () {
    echo -E - $''\0''
}
compprefuncs=( null-line )
comppostfuncs=( null-line exit )

# never group stuff!
zstyle '':completion:*'' list-grouped false
# don''t insert tab when attempting completion on empty line
zstyle '':completion:*'' insert-tab false
# no list separator, this saves some stripping later on
zstyle '':completion:*'' list-separator ''''

# load requirements for zparseopts
zmodload zsh/zutil

# override compadd (this our hook)
compadd () {

    # check if any of -O, -A or -D are given
    if [[ ${@[1,(i)(-|--)]} == *-(O|A|D)\ * ]]; then
        # if that is the case, just delegate and leave
        builtin compadd "$@"
        return $?
    fi

    # ok, this concerns us!
    # echo -E - got this: "$@"

    # be careful with namespacing here, we don''t want to mess with stuff that
    # should be passed to compadd!
    typeset -a __hits __dscr __tmp

    # do we have a description parameter?
    # note we don''t use zparseopts here because of combined option parameters
    # with arguments like -default- confuse it.
    if (( $@[(I)-d] )); then # kind of a hack, $+@[(r)-d] doesn''t work because of line noise overload
        # next param after -d
        __tmp=${@[$[${@[(i)-d]}+1]]}
        # description can be given as an array parameter name, or inline () array
        if [[ $__tmp == \(* ]]; then
            eval "__dscr=$__tmp"
        else
            __dscr=( "${(@P)__tmp}" )
        fi
    fi

    # capture completions by injecting -A parameter into the compadd call.
    # this takes care of matching for us.
    builtin compadd -A __hits -D __dscr "$@"

    # set sane default options
    setopt localoptions norcexpandparam extendedglob

    # extract prefixes and suffixes from compadd call. we can''t do zsh''s cool
    # -r remove-func magic, but it''s better than nothing.
    typeset -A apre hpre hsuf asuf
    zparseopts -E P:=apre p:=hpre S:=asuf s:=hsuf

    # append / to directories? we are only emulating -f.
    integer dirsuf=0
    if [[ -z $hsuf && "${${@//-default-/}% -# *}" == *-[[:alnum:]]#f* ]]; then
        dirsuf=1
    fi

    # just drop
    [[ -n $__hits ]] || return

    # this is the point where we have all matches in $__hits and all
    # descriptions in $__dscr!

    # display all matches
    local dsuf dscr
    for i in {1..$#__hits}; do

        # add a dir suffix?
        (( dirsuf )) && [[ -d $__hits[$i] ]] && dsuf=/ || dsuf=
        # description to be displayed afterwards
        (( $#__dscr >= $i )) && dscr=" -- ${${__dscr[$i]}##$__hits[$i] #}" || dscr=

        echo -E - $IPREFIX$apre$hpre$__hits[$i]$dsuf$hsuf$asuf$dscr

    done
}

# signal success!
echo ok')

zpty -w z "$*"$'\t'

integer tog=0
# read from the pty, and parse linewise
while zpty -r z; do :; done | while IFS= read -r line; do
    if [[ $line == *$'\0\r' ]]; then
        (( tog++ )) && return 0 || continue
    fi
    # display between toggles
    (( tog )) && echo -E - $line
done

return 2
}"""
"""
ZSH capture completion

https://github.com/Valodim/zsh-capture-completion

Description

Roughly, a pseudo-interactive zsh session is spawned using zpty, and a buffer
string plus a tab character is sent so the complete-word widget is executed.
To capture the hits, the compadd function is selectivly overridden in an
inline-sourced file, capturing matches by injecting a parameter to the
original compadd call and outputting matches to stdout.
"""


def plugin_loaded():
    """
    Generate a list of known words, provided by static completion files
    of ST's ShellScript package. It contains keywords, built-in commands and
    variables, which don't need to be provided by this plugin and would
    otherwise cause duplicates.
    """
    global KNOWN_COMPLETIONS
    KNOWN_COMPLETIONS = set()

    for res in sublime.find_resources("*.sublime-completions"):
        if res.startswith("Packages/ShellScript/"):
            data = sublime.decode_value(sublime.load_resource(res))
            if sublime.score_selector("source.shell.zsh", data["scope"].split(" ", 1)[0]) > 0:
                for item in data["completions"]:
                    trigger = item.get("trigger")
                    if trigger:
                        KNOWN_COMPLETIONS.add(trigger)


class ZshCompletionListener(sublime_plugin.EventListener):
    enabled = True

    def on_query_completions(self, view: sublime.View, prefix: str, locations: list[sublime.Point]):
        if not self.enabled:
            return None

        pt = locations[0]
        if not view.match_selector(pt, "source.shell - comment - string.quoted"):
            return None

        completions_list = sublime.CompletionList(None)

        def get_completions():
            file_name = view.file_name()
            cwd = os.path.dirname(file_name) if file_name else None

            info = None
            if os.name == 'nt':
                info = subprocess.STARTUPINFO()
                info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                info.wShowWindow = subprocess.SW_HIDE

            try:
                data = subprocess.check_output(
                    executable="zsh",
                    args=ZSH_CAPTURE_COMPLETION + " " + view.substr(view.line(pt)),
                    cwd=cwd,
                    shell=True,
                    startupinfo=info,
                    timeout=4.0
                )
                if data is None:
                   data = b''

            except subprocess.TimeoutExpired:
                return

            except subprocess.CalledProcessError as e:
                if e.returncode in (1, 2):
                    self.enabled = False
                    print("ZSH Completion initialization failed, disabling completions!")
                return

            except FileNotFoundError:
                self.enabled = False
                print("ZSH not found, disabling completions!")
                return

            completions_list.set_completions(self.completion_items(data, prefix))

        sublime.set_timeout_async(get_completions)
        return completions_list

    @staticmethod
    def completion_items(data, prefix):
        kind = [sublime.KindId.NAMESPACE, "f", "Filesystem"]
        found = set()
        for line in str(data, encoding="utf-8").split("\r\n"):
            parts = line.split(" -- ", 1)
            word = parts[0]

            if word in KNOWN_COMPLETIONS:
                continue
            if word in found:
                continue
            found.add(word)

            yield sublime.CompletionItem(
                trigger=word,
                annotation="ZSH",
                kind=kind,
                details=parts[1] if len(parts) > 1 else "file or folder"
            )

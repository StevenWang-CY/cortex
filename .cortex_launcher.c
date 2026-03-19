/* Minimal launcher for the Cortex daemon.
   Compiled into CortexDaemon.app so macOS gives it its own TCC identity
   (com.cortex.daemon) for camera access.

   Retries chdir on failure because macOS may need to show a Desktop folder
   access dialog the first time this app runs. */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(void) {
    const char *project = "/Users/chuyuewang/Desktop/CS/Project/Ralph";
    const char *python  = "/Users/chuyuewang/Desktop/CS/Project/Ralph/.venv/bin/python";
    const char *logfile = "/Users/chuyuewang/Desktop/CS/Project/Ralph/cortex_daemon.log";

    /* Retry chdir — macOS TCC may prompt for Desktop folder access.
       Give the user up to 15 seconds to click Allow. */
    int cd_ok = 0;
    for (int i = 0; i < 15; i++) {
        if (chdir(project) == 0) {
            cd_ok = 1;
            break;
        }
        sleep(1);
    }

    /* Redirect stdout/stderr to log file (use /tmp if project dir inaccessible) */
    const char *actual_log = cd_ok ? logfile : "/tmp/cortex_daemon.log";
    FILE *log = fopen(actual_log, "a");
    if (log) {
        dup2(fileno(log), STDOUT_FILENO);
        dup2(fileno(log), STDERR_FILENO);
        fclose(log);
    }

    if (!cd_ok) {
        fprintf(stderr, "ERROR: Could not chdir to %s after 15s\n", project);
        return 1;
    }

    char *argv[] = {(char *)python, "-m", "cortex.scripts.run_dev", NULL};
    execv(python, argv);

    /* execv only returns on error */
    perror("execv");
    return 1;
}

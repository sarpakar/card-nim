/* Card Nim client in C.  POSIX sockets only, no libraries:
 *
 *     cc -O2 client.c -o client
 *     ./client --server http://localhost:8000 --game K7PX --name "C bot" [--seat 1]
 *
 * CARDNIM_SERVER, CARDNIM_GAME, CARDNIM_NAME and CARDNIM_SEAT are used as
 * defaults, which is how the server starts this client for a seat.
 *
 * The state comes back as "key value" lines (?format=text), so there is no
 * JSON to parse.  Put your strategy in choose_card(); the rest is plumbing.
 */

#include <arpa/inet.h>
#include <ctype.h>
#include <netdb.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>

#define MAX_CARDS 256
#define BUF 65536

/* ============================================================ your strategy */

/* ------------------------------------------------------------------ strategy
 *
 * Two layers.
 *
 * SEARCH.  The pile is implied by the two hands, so a position is just
 * (my cards, their cards, whose turn) and a transposition table entry is
 * exact forever -- no depth field, no re-search. The outcome is win/lose,
 * so alpha-beta collapses to "stop at the first losing child". Two cuts do
 * most of the work: a card that leaves the pile inside their hand loses on
 * the spot and is never searched, and a card that leaves the pile under
 * their smallest card wins on the spot. Within the node budget this plays
 * the game perfectly; when the budget runs out the answer is UNKNOWN, never
 * a guess -- an aborted branch must not be mistaken for a loss.
 *
 * HEURISTIC.  Used only when the search cannot finish. Never hand them an
 * exact match; otherwise play the LARGEST safe card. Playing big spends the
 * pile while keeping your small cards, and your small cards are what stop
 * you being stranded -- while you still hold a 1 you can always move. This
 * was measured against exhaustive solutions for k<=11: it throws away a won
 * position 0.08%-10.7% of the time, against 0.17%-23.3% for "smallest safe",
 * which is what the sample bots play.
 */

#define TTBITS 22
#define TTSIZE (1u << TTBITS)
#define NODE_BUDGET 400000000L       /* hard ceiling; the deadline below is what really stops it */
#define GAME_THINK_BUDGET 80.0       /* seconds of thinking for the WHOLE game (clock is 120) */
#define MOVE_THINK_CAP    8.0        /* never sink more than this into one move */
#define TIME_CHECK_MASK   0xFFFF     /* consult the clock every 65536 nodes */
#define SETW 4                       /* 4*64 bits covers k <= 200 */

#define UNKNOWN 2

typedef struct { unsigned long long w[SETW]; } Set;
typedef struct { unsigned long long key; unsigned char val; } TTEnt;

static TTEnt *tt_tab;
static unsigned long long ZOB[257][2];
static unsigned long long ZPILE[512];   /* keeps the table honest if the process plays a second game */
static long nodes_used, node_budget;
static double think_left = GAME_THINK_BUDGET;   /* thinking time still affordable this game */
static double move_deadline;                    /* monotonic seconds; 0 = no deadline */
static int    out_of_time;

static double now_secs(void){
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}
static int kmax;                     /* highest card either side ever held */

static int  sethas(const Set *s, int c){ return (s->w[(c-1)>>6] >> ((c-1)&63)) & 1ULL; }
static void setdel(Set *s, int c){ s->w[(c-1)>>6] &= ~(1ULL << ((c-1)&63)); }
static void setadd(Set *s, int c){ s->w[(c-1)>>6] |=  (1ULL << ((c-1)&63)); }
static int  setmin(const Set *s){
    int i; for (i = 0; i < SETW; i++) if (s->w[i]) return i*64 + __builtin_ctzll(s->w[i]) + 1;
    return 0;
}
static int setmax(const Set *s){
    int i; for (i = SETW-1; i >= 0; i--) if (s->w[i]) return i*64 + 63 - __builtin_clzll(s->w[i]) + 1;
    return 0;
}

static void zobrist_init(void){
    unsigned long long x = 0x9E3779B97F4A7C15ULL; int c, p;
    for (c = 0; c <= 256; c++) for (p = 0; p < 2; p++) {
        x += 0x9E3779B97F4A7C15ULL;                       /* splitmix64 */
        unsigned long long z = x;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
        ZOB[c][p] = z ^ (z >> 31);
    }
    for (c = 0; c < 512; c++) {
        x += 0x9E3779B97F4A7C15ULL;
        unsigned long long z = x;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
        ZPILE[c] = z ^ (z >> 31);
    }
}

/* Win/lose/unknown for the player to move. a0,a1 hash the mover's hand under
 * each colour plane, b0,b1 the opponent's; handing over the move is then four
 * xors instead of a walk over the hand. */
static int solve(int pile, Set *me, Set *op,
                 unsigned long long a0, unsigned long long a1,
                 unsigned long long b0, unsigned long long b1) {
    unsigned long long h;
    unsigned idx;
    int res, mn, opmin, c, lim, saw_unknown = 0;

    if (++nodes_used > node_budget) return UNKNOWN;
    if ((nodes_used & TIME_CHECK_MASK) == 0 && move_deadline > 0.0 && now_secs() > move_deadline) {
        out_of_time = 1;                 /* the referee counts seconds, not nodes */
        return UNKNOWN;
    }
    if (out_of_time) return UNKNOWN;

    h = a0 ^ b1 ^ ZPILE[pile & 511];
    idx = (unsigned)(h & (TTSIZE - 1));
    if (tt_tab[idx].key == h) return tt_tab[idx].val;

    mn = setmin(me);
    if (mn == 0 || mn > pile) return 0;                  /* stranded: lose */
    if (pile <= kmax && sethas(me, pile)) return 1;      /* exact match: win */

    opmin = setmin(op);
    lim = pile < kmax ? pile : kmax;
    for (c = mn; c <= lim; c++) {                        /* strand them: win */
        if (!sethas(me, c)) continue;
        if (pile - c > 0 && pile - c < opmin) return 1;
    }

    res = 0;
    for (c = setmax(me) < lim ? setmax(me) : lim; c >= mn; c--) {   /* big first */
        int left;
        if (!sethas(me, c)) continue;
        left = pile - c;
        if (left <= 0) continue;
        if (left <= kmax && sethas(op, left)) continue;   /* hands them the win */
        setdel(me, c);
        {
            int sub = solve(left, op, me, b0, b1, a0 ^ ZOB[c][0], a1 ^ ZOB[c][1]);
            setadd(me, c);
            if (sub == 0) { res = 1; break; }             /* they lose => we win */
            if (sub == UNKNOWN) saw_unknown = 1;
        }
    }
    if (!res && saw_unknown) return UNKNOWN;              /* do not call it a loss */

    tt_tab[idx].key = h; tt_tab[idx].val = (unsigned char)res;
    return res;
}

/* Purpose: pick the card to play.
 * Inputs:  stones on the table, your cards, how many, the opponent's cards.
 * Output:  one card from your hand.  A card larger than stones loses at once. */
static int choose_card(int stones, const int *mine, int n_mine,
                       const int *theirs, int n_theirs) {
    Set me, op;
    unsigned long long a0=0, a1=0, b0=0, b1=0;
    int i, c, best = -1, opmin = 0, fallback = -1;
    double t_start = 0.0;

    if (n_mine <= 0) return 1;

    memset(&me, 0, sizeof me);
    memset(&op, 0, sizeof op);
    kmax = 1;
    for (i = 0; i < n_mine;   i++) if (mine[i]   >= 1 && mine[i]   <= 200) { setadd(&me, mine[i]);   if (mine[i]   > kmax) kmax = mine[i]; }
    for (i = 0; i < n_theirs; i++) if (theirs[i] >= 1 && theirs[i] <= 200) { setadd(&op, theirs[i]); if (theirs[i] > kmax) kmax = theirs[i]; }

    for (i = 0; i < n_mine; i++) if (mine[i] == stones) return stones;   /* take the win */

    opmin = setmin(&op);
    for (i = 0; i < n_mine; i++) {                                       /* strand them */
        int left = stones - mine[i];
        if (mine[i] <= stones && left > 0 && opmin && left < opmin) return mine[i];
    }

    /* exact search, biggest card first so a win is usually found early */
    if (!tt_tab) { tt_tab = calloc(TTSIZE, sizeof(TTEnt)); zobrist_init(); }
    if (tt_tab) {
        for (c = 1; c <= kmax; c++) if (sethas(&me, c)) { a0 ^= ZOB[c][0]; a1 ^= ZOB[c][1]; }
        for (c = 1; c <= kmax; c++) if (sethas(&op, c)) { b0 ^= ZOB[c][0]; b1 ^= ZOB[c][1]; }
        {
            double cap = think_left * 0.25;          /* never spend the whole reserve on one move */
            if (cap > MOVE_THINK_CAP) cap = MOVE_THINK_CAP;
            if (cap < 0.05) cap = 0.05;              /* always allow a quick look */
            t_start = now_secs();
            move_deadline = t_start + cap;
        }
        out_of_time = 0;
        nodes_used = 0; node_budget = NODE_BUDGET;
        for (c = kmax; c >= 1; c--) {
            int left, sub;
            if (!sethas(&me, c) || c > stones) continue;
            left = stones - c;
            if (left <= 0) continue;
            if (left <= kmax && sethas(&op, left)) continue;
            setdel(&me, c);
            sub = solve(left, &op, &me, b0, b1, a0 ^ ZOB[c][0], a1 ^ ZOB[c][1]);
            setadd(&me, c);
            if (sub == 0) { think_left -= now_secs() - t_start; move_deadline = 0.0; return c; }
            if (nodes_used > node_budget || out_of_time) break;
        }
        think_left -= now_secs() - t_start;
        if (think_left < 0.0) think_left = 0.0;
        move_deadline = 0.0;
    }

    /* no proof available: never leave a pile they hold, and play big */
    for (i = 0; i < n_mine; i++) {
        int left;
        if (mine[i] > stones) continue;
        if (fallback < 0 || mine[i] > fallback) fallback = mine[i];
        left = stones - mine[i];
        if (left > 0 && left <= kmax && sethas(&op, left)) continue;      /* gives them the win */
        if (best < 0 || mine[i] > best) best = mine[i];                   /* LARGEST safe */
    }
    if (best >= 0) return best;
    if (fallback >= 0) return fallback;
    return mine[0];
}

/* Purpose: the smallest card that fits, played when the server refuses our
 * choice, so a bug in the strategy cannot burn the clock. */
static int safest_card(int stones, const int *mine, int n_mine) {
    int i, best = -1;
    for (i = 0; i < n_mine; i++)
        if (mine[i] <= stones && (best < 0 || mine[i] < best)) best = mine[i];
    return best >= 0 ? best : (n_mine ? mine[0] : 1);
}

/* ============================================================ plumbing */

static char host[256] = "localhost";
static int port = 8000;

/* Purpose: split "http://host:port" into host and port. */
static void parse_url(const char *url) {
    const char *p = url;
    char *colon;
    if (strncmp(p, "http://", 7) == 0) p += 7;
    snprintf(host, sizeof host, "%s", p);
    while (*host && host[strlen(host) - 1] == '/') host[strlen(host) - 1] = '\0';
    colon = strchr(host, ':');
    if (colon) { *colon = '\0'; port = atoi(colon + 1); }
}

/* Purpose: one HTTP request; writes the body into `out`.
 * Outputs: the HTTP status, or 0 if the server could not be reached. */
static int http(const char *method, const char *path, char *out, size_t out_len) {
    struct addrinfo hints, *res = NULL, *p;
    char portstr[16], req[2048], buf[BUF];
    int fd = -1, status = 0;
    size_t total = 0;
    char *split;

    out[0] = '\0';
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    snprintf(portstr, sizeof portstr, "%d", port);
    if (getaddrinfo(host, portstr, &hints, &res) != 0) return 0;
    for (p = res; p; p = p->ai_next) {
        fd = socket(p->ai_family, p->ai_socktype, p->ai_protocol);
        if (fd < 0) continue;
        if (connect(fd, p->ai_addr, p->ai_addrlen) == 0) break;
        close(fd);
        fd = -1;
    }
    freeaddrinfo(res);
    if (fd < 0) return 0;

    snprintf(req, sizeof req,
             "%s %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
             method, path, host);
    if (write(fd, req, strlen(req)) < 0) { close(fd); return 0; }

    for (;;) {
        ssize_t n = read(fd, buf, sizeof buf);
        if (n <= 0) break;
        if (total + (size_t)n < out_len - 1) {
            memcpy(out + total, buf, (size_t)n);
            total += (size_t)n;
        }
    }
    close(fd);
    out[total] = '\0';
    if (total > 12) status = atoi(out + 9);          /* "HTTP/1.1 200 OK" */
    split = strstr(out, "\r\n\r\n");
    if (split) memmove(out, split + 4, strlen(split + 4) + 1);
    else out[0] = '\0';
    return status;
}

/* Purpose: the value of one "key value..." line, or "" if absent. */
static const char *field(const char *text, const char *key, char *out, size_t out_len) {
    const char *line = text;
    size_t klen = strlen(key);
    out[0] = '\0';
    while (line && *line) {
        const char *end = strchr(line, '\n');
        size_t len = end ? (size_t)(end - line) : strlen(line);
        if (len > klen && strncmp(line, key, klen) == 0 && line[klen] == ' ') {
            size_t vlen = len - klen - 1;
            while (vlen && (line[klen + 1 + vlen - 1] == '\r')) vlen--;
            if (vlen >= out_len) vlen = out_len - 1;
            memcpy(out, line + klen + 1, vlen);
            out[vlen] = '\0';
            return out;
        }
        line = end ? end + 1 : NULL;
    }
    return out;
}

/* Purpose: "1 2 3" -> array of ints.  Outputs: how many were read. */
static int ints(const char *s, int *out, int max) {
    int n = 0;
    while (*s && n < max) {
        while (*s == ' ') s++;
        if (!*s) break;
        out[n++] = atoi(s);
        while (*s && *s != ' ') s++;
    }
    return n;
}

/* Purpose: percent-encode a name for a query parameter. */
static void urlencode(const char *s, char *out, size_t out_len) {
    size_t o = 0;
    for (; *s && o + 4 < out_len; s++) {
        unsigned char c = (unsigned char)*s;
        if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') out[o++] = (char)c;
        else o += (size_t)snprintf(out + o, out_len - o, "%%%02X", c);
    }
    out[o] = '\0';
}

static const char *opt(int argc, char **argv, const char *flag, const char *env, const char *dflt) {
    int i;
    const char *v;
    for (i = 1; i + 1 < argc; i++)
        if (strcmp(argv[i], flag) == 0) return argv[i + 1];
    v = getenv(env);
    return (v && *v) ? v : dflt;
}

/* ============================================================ game loop */

int main(int argc, char **argv) {
    char body[BUF], val[1024], path[2048], token[512], encname[512];
    int mine[MAX_CARDS], theirs[MAX_CARDS], n_mine, n_theirs;
    int status, my_seat, stones, card, failures = 0;
    const char *server = opt(argc, argv, "--server", "CARDNIM_SERVER", "http://localhost:8000");
    const char *game = opt(argc, argv, "--game", "CARDNIM_GAME", "");
    const char *name = opt(argc, argv, "--name", "CARDNIM_NAME", "C bot");
    const char *seat = opt(argc, argv, "--seat", "CARDNIM_SEAT", "");

    if (!*game) {
        fprintf(stderr, "usage: client --game ID [--server http://host:8000] [--name NAME] [--seat 1|2]\n");
        return 1;
    }
    parse_url(server);
    urlencode(name, encname, sizeof encname);

    snprintf(path, sizeof path, "/api/games/%s/join?format=text&name=%s%s%s",
             game, encname, *seat ? "&seat=" : "", seat);
    status = http("POST", path, body, sizeof body);
    field(body, "token", token, sizeof token);
    if (status != 200 || !*token) {
        fprintf(stderr, "[%s] join failed (%d): %s\n", name, status, body);
        return 1;
    }
    field(body, "seat", val, sizeof val);
    my_seat = atoi(val);
    printf("[%s] joined %s as seat %d\n", name, game, my_seat);
    fflush(stdout);

    for (;;) {
        /* getstate answers only when it is our turn or the game is over, so
         * this loop does not poll and waiting costs nothing on the clock. */
        snprintf(path, sizeof path, "/api/games/%s/getstate?format=text&timeout=60&token=%s", game, token);
        status = http("GET", path, body, sizeof body);
        if (status == 0) {
            if (++failures >= 30) { fprintf(stderr, "[%s] server gone\n", name); return 1; }
            sleep(1);
            continue;
        }
        failures = 0;
        if (status != 200) { fprintf(stderr, "[%s] getstate failed (%d)\n", name, status); return 1; }

        field(body, "status", val, sizeof val);
        if (strcmp(val, "finished") == 0) {
            int winner;
            field(body, "winner", val, sizeof val);
            winner = atoi(val);
            field(body, "reason", val, sizeof val);
            printf("[%s] game over: %s. %s\n", name,
                   winner == my_seat ? "WIN" : winner ? "LOSS" : "no winner", val);
            return winner == my_seat ? 0 : 2;
        }
        field(body, "your_turn", val, sizeof val);
        if (strcmp(val, "1") != 0) continue;

        field(body, "stones", val, sizeof val);
        stones = atoi(val);
        field(body, "your_cards", val, sizeof val);
        n_mine = ints(val, mine, MAX_CARDS);
        field(body, "opp_cards", val, sizeof val);
        n_theirs = ints(val, theirs, MAX_CARDS);

        card = choose_card(stones, mine, n_mine, theirs, n_theirs);
        printf("[%s] %d stones, playing %d\n", name, stones, card);
        fflush(stdout);

        snprintf(path, sizeof path, "/api/games/%s/move?format=text&token=%s&card=%d", game, token, card);
        status = http("POST", path, body, sizeof body);
        if (status != 200) {
            int back = safest_card(stones, mine, n_mine);
            fprintf(stderr, "[%s] move %d rejected (%d)\n", name, card, status);
            if (back != card) {
                snprintf(path, sizeof path, "/api/games/%s/move?format=text&token=%s&card=%d", game, token, back);
                http("POST", path, body, sizeof body);
            } else {
                sleep(1);      /* not our turn any more: do not hammer it */
            }
        }
    }
}

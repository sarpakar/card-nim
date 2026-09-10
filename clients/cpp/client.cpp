// Card Nim C++ client.  No libraries beyond POSIX sockets; builds on macOS and Linux:
//
//     g++ -std=c++17 -O2 client.cpp -o client
//     ./client --server http://localhost:8000 --game K7PX --name "C++ bot" [--seat 1]
//
// Environment variables CARDNIM_SERVER, CARDNIM_GAME, CARDNIM_NAME, CARDNIM_SEAT are
// used as defaults (scripts/run_match.py sets them).
//
// The client talks HTTP/1.1 with "Connection: close" and asks the server for the
// plain-text state format (?format=text), so no JSON parser is needed.  Each line
// of that format is "key value...":
//
//     status playing | waiting | finished
//     you 2                    your seat
//     turn 2                   seat to move (0 if not playing)
//     your_turn 1              1 or 0
//     stones 37
//     your_cards 1 2 4 5
//     opp_cards 1 3 6
//     your_time 118.320        seconds left on your clock
//     opp_time 119.100
//     last_move 1 3            seat and card of the last move (0 0 if none)
//     winner 0                 0, 1 or 2
//     reason ...
//
// Put your strategy in choose_card() below.  Everything else is plumbing.

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

// ============================================================ your strategy

// Purpose: pick the card to play.
// Inputs:  stones on the table, your remaining cards, the opponent's remaining cards.
// Output:  a card from your hand.  A card larger than `stones` loses on the spot.
static int choose_card(int stones, const std::vector<int>& my_cards, const std::vector<int>& opp_cards) {
    // Sample strategy: win if possible; otherwise avoid leaving a number the
    // opponent holds; otherwise play the smallest fitting card.
    for (int c : my_cards) if (c == stones) return c;
    int best = -1;
    for (int c : my_cards) {
        if (c > stones) continue;
        bool opp_can_win = false;
        for (int o : opp_cards) if (o == stones - c) opp_can_win = true;
        if (!opp_can_win && (best < 0 || c < best)) best = c;
    }
    if (best >= 0) return best;
    for (int c : my_cards) if (c <= stones && (best < 0 || c < best)) best = c;
    if (best >= 0) return best;
    return my_cards.empty() ? 1 : my_cards.front();   // nothing fits: any card loses, never crash
}

// Purpose: the smallest card that fits, used if choose_card() produced a card
// the server refused (so a bug in the strategy cannot burn the clock).
static int safest_card(int stones, const std::vector<int>& my_cards) {
    int best = -1;
    for (int c : my_cards) if (c <= stones && (best < 0 || c < best)) best = c;
    if (best >= 0) return best;
    return my_cards.empty() ? 1 : my_cards.front();
}

// ============================================================ HTTP plumbing

struct Url { std::string host; int port = 80; };

static Url parse_url(const std::string& server) {
    Url u;
    std::string rest = server;
    if (rest.rfind("http://", 0) == 0) rest = rest.substr(7);
    while (!rest.empty() && rest.back() == '/') rest.pop_back();
    size_t colon = rest.find(':');
    if (colon == std::string::npos) { u.host = rest; }
    else { u.host = rest.substr(0, colon); u.port = std::atoi(rest.c_str() + colon + 1); }
    return u;
}

// Purpose: one HTTP request; returns the response body (empty string on connection failure).
// `status` receives the HTTP status code.
static std::string http(const Url& u, const std::string& method, const std::string& path,
                        const std::string& body, int& status) {
    status = 0;
    struct addrinfo hints{}, *res = nullptr;
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(u.host.c_str(), std::to_string(u.port).c_str(), &hints, &res) != 0) {
        std::cerr << "cannot resolve " << u.host << "\n";
        return "";
    }
    int fd = -1;
    for (auto* p = res; p; p = p->ai_next) {
        fd = socket(p->ai_family, p->ai_socktype, p->ai_protocol);
        if (fd < 0) continue;
        if (connect(fd, p->ai_addr, p->ai_addrlen) == 0) break;
        close(fd); fd = -1;
    }
    freeaddrinfo(res);
    if (fd < 0) { std::cerr << "cannot connect to " << u.host << ":" << u.port << "\n"; return ""; }

    std::ostringstream req;
    req << method << " " << path << " HTTP/1.1\r\n"
        << "Host: " << u.host << "\r\n"
        << "Connection: close\r\n"
        << "Content-Type: application/x-www-form-urlencoded\r\n"
        << "Content-Length: " << body.size() << "\r\n\r\n"
        << body;
    std::string out = req.str();
    size_t sent = 0;
    while (sent < out.size()) {
        ssize_t n = send(fd, out.data() + sent, out.size() - sent, 0);
        if (n <= 0) { close(fd); return ""; }
        sent += (size_t)n;
    }
    std::string resp;
    char buf[4096];
    for (;;) {
        ssize_t n = recv(fd, buf, sizeof buf, 0);
        if (n <= 0) break;
        resp.append(buf, (size_t)n);
    }
    close(fd);
    // "HTTP/1.1 200 OK\r\n..." -> status; body follows the blank line.
    if (resp.size() > 12) status = std::atoi(resp.c_str() + 9);
    size_t split = resp.find("\r\n\r\n");
    return split == std::string::npos ? "" : resp.substr(split + 4);
}

static std::string url_encode(const std::string& s) {
    std::ostringstream o;
    for (unsigned char c : s) {
        if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') o << c;
        else { char h[4]; std::snprintf(h, sizeof h, "%%%02X", c); o << h; }
    }
    return o.str();
}

// Purpose: parse the "key value..." text format into a map.
static std::map<std::string, std::string> parse_state(const std::string& text) {
    std::map<std::string, std::string> m;
    std::istringstream in(text);
    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        size_t sp = line.find(' ');
        if (sp == std::string::npos) m[line] = "";
        else m[line.substr(0, sp)] = line.substr(sp + 1);
    }
    return m;
}

static std::vector<int> ints(const std::string& s) {
    std::vector<int> v;
    std::istringstream in(s);
    int x;
    while (in >> x) v.push_back(x);
    return v;
}

// ============================================================ game loop

int main(int argc, char** argv) {
    auto env = [](const char* k, const char* d) { const char* v = std::getenv(k); return std::string(v ? v : d); };
    std::string server = env("CARDNIM_SERVER", "http://localhost:8000");
    std::string game = env("CARDNIM_GAME", "");
    std::string name = env("CARDNIM_NAME", "C++ bot");
    std::string seat = env("CARDNIM_SEAT", "");
    for (int i = 1; i + 1 < argc; i += 2) {
        std::string a = argv[i];
        if (a == "--server") server = argv[i + 1];
        else if (a == "--game") game = argv[i + 1];
        else if (a == "--name") name = argv[i + 1];
        else if (a == "--seat") seat = argv[i + 1];
    }
    if (game.empty()) {
        std::cerr << "usage: client --server http://host:8000 --game ID --name NAME [--seat 1|2]\n";
        return 1;
    }
    Url u = parse_url(server);
    std::string base = "/api/games/" + game;
    int status = 0;

    // join
    std::string q = "?format=text&name=" + url_encode(name) + (seat.empty() ? "" : "&seat=" + seat);
    auto joined = parse_state(http(u, "POST", base + "/join" + q, "", status));
    if (status != 200) { std::cerr << "join failed (" << status << "): " << joined["error"] << "\n"; return 1; }
    std::string token = joined["token"];
    int my_seat = std::atoi(joined["seat"].c_str());
    std::cout << "[" << name << "] joined " << game << " as seat " << my_seat << "\n";

    int failures = 0;
    for (;;) {
        // getstate: returns when it is our turn or the game is over (or after 60 s, then we ask again)
        auto st = parse_state(http(u, "GET", base + "/getstate?format=text&timeout=60&token=" + token, "", status));
        if (status == 0) {                       // server unreachable: retry for a while, then give up
            if (++failures >= 30) { std::cerr << "[" << name << "] server gone, giving up\n"; return 1; }
            sleep(1); continue;
        }
        failures = 0;
        if (status != 200) { std::cerr << "getstate failed (" << status << ")\n"; return 1; }
        if (st["status"] == "finished") {
            int winner = std::atoi(st["winner"].c_str());
            std::cout << "[" << name << "] game over: " << (winner == my_seat ? "WIN" : winner ? "LOSS" : "no winner")
                      << ". " << st["reason"] << "\n";
            return winner == my_seat ? 0 : 2;
        }
        if (st["your_turn"] != "1") continue;

        int stones = std::atoi(st["stones"].c_str());
        std::vector<int> mine = ints(st["your_cards"]);
        std::vector<int> theirs = ints(st["opp_cards"]);
        int card = choose_card(stones, mine, theirs);
        std::cout << "[" << name << "] " << stones << " stones, playing " << card << "\n";

        auto after = parse_state(http(u, "POST", base + "/move?format=text&token=" + token, "card=" + std::to_string(card), status));
        if (status != 200) {
            std::cerr << "[" << name << "] move " << card << " rejected (" << status << "): " << after["error"] << "\n";
            int fallback = safest_card(stones, mine);
            if (fallback != card) {
                std::cerr << "[" << name << "] playing " << fallback << " instead\n";
                http(u, "POST", base + "/move?format=text&token=" + token, "card=" + std::to_string(fallback), status);
            }
            if (status != 200) sleep(1);   // not our turn any more or server hiccup: do not hammer it
        }
    }
}

// Card Nim Java client.  Needs Java 11 or newer (java.net.http), nothing else.
//
//     javac Client.java
//     java Client --server http://localhost:8000 --game K7PX --name "Java bot" [--seat 1]
//
// Environment variables CARDNIM_SERVER, CARDNIM_GAME, CARDNIM_NAME, CARDNIM_SEAT are
// used as defaults (scripts/run_match.py sets them).
//
// Uses the plain-text state format (?format=text) so no JSON library is needed.
// See clients/cpp/client.cpp or docs/API.md for the list of keys.
// Put your strategy in chooseCard(); everything else is plumbing.

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class Client {

    // ============================================================ your strategy

    /** Purpose: pick the card to play.
     *  Inputs: stones on the table, your remaining cards, the opponent's remaining cards.
     *  Output: a card from your hand.  A card larger than stones loses on the spot. */
    static int chooseCard(int stones, List<Integer> myCards, List<Integer> oppCards) {
        // Sample strategy: win if possible; otherwise avoid leaving a number the
        // opponent holds; otherwise the smallest fitting card.
        if (myCards.contains(stones)) return stones;
        int best = -1;
        for (int c : myCards) {
            if (c > stones) continue;
            if (!oppCards.contains(stones - c) && (best < 0 || c < best)) best = c;
        }
        if (best >= 0) return best;
        for (int c : myCards) if (c <= stones && (best < 0 || c < best)) best = c;
        if (best >= 0) return best;
        return myCards.isEmpty() ? 1 : myCards.get(0);   // nothing fits: any card loses, never crash
    }

    /** The smallest card that fits: played if chooseCard() returned something the server refused. */
    static int safestCard(int stones, List<Integer> myCards) {
        int best = -1;
        for (int c : myCards) if (c <= stones && (best < 0 || c < best)) best = c;
        if (best >= 0) return best;
        return myCards.isEmpty() ? 1 : myCards.get(0);
    }

    // ============================================================ plumbing

    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
    private final String base;
    private final String name;
    private String token;
    private int seat;

    Client(String server, String gameId, String name) {
        while (server.endsWith("/")) server = server.substring(0, server.length() - 1);
        this.base = server + "/api/games/" + gameId.toUpperCase();
        this.name = name;
    }

    /** One HTTP call; returns [statusCode, body]. */
    private Object[] call(String method, String path, String body) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + path))
                .timeout(Duration.ofSeconds(90))
                .header("Content-Type", "application/x-www-form-urlencoded");
        if (token != null) b.header("X-Token", token);
        if (method.equals("POST")) b.POST(HttpRequest.BodyPublishers.ofString(body == null ? "" : body));
        else b.GET();
        HttpResponse<String> r = http.send(b.build(), HttpResponse.BodyHandlers.ofString());
        return new Object[]{r.statusCode(), r.body()};
    }

    /** Parse the "key value..." text format into a map. */
    static Map<String, String> parse(String text) {
        Map<String, String> m = new HashMap<>();
        for (String line : text.split("\n")) {
            line = line.replace("\r", "");
            int sp = line.indexOf(' ');
            if (sp < 0) m.put(line, "");
            else m.put(line.substring(0, sp), line.substring(sp + 1));
        }
        return m;
    }

    static List<Integer> ints(String s) {
        List<Integer> v = new ArrayList<>();
        if (s == null) return v;
        for (String p : s.trim().split("\\s+")) if (!p.isEmpty()) v.add(Integer.parseInt(p));
        return v;
    }

    static String enc(String s) { return URLEncoder.encode(s, StandardCharsets.UTF_8); }

    void join(String seatPref) throws Exception {
        String q = "?format=text&name=" + enc(name) + (seatPref.isEmpty() ? "" : "&seat=" + seatPref);
        Object[] r = call("POST", "/join" + q, "");
        Map<String, String> m = parse((String) r[1]);
        if ((int) r[0] != 200) throw new RuntimeException("join failed: " + m.getOrDefault("error", (String) r[1]));
        token = m.get("token");
        seat = Integer.parseInt(m.get("seat"));
        System.out.println("[" + name + "] joined as seat " + seat);
    }

    /** Runs the whole game.  Returns 0 on a win, 2 otherwise. */
    int play() throws Exception {
        while (true) {
            Object[] r = call("GET", "/getstate?format=text&timeout=60&token=" + token, null);
            if ((int) r[0] != 200) throw new RuntimeException("getstate failed: " + r[1]);
            Map<String, String> st = parse((String) r[1]);
            if (st.get("status").equals("finished")) {
                int winner = Integer.parseInt(st.get("winner"));
                System.out.println("[" + name + "] game over: " + (winner == seat ? "WIN" : winner == 0 ? "no winner" : "LOSS")
                        + ". " + st.get("reason"));
                return winner == seat ? 0 : 2;
            }
            if (!st.get("your_turn").equals("1")) continue;   // server gave up waiting; ask again

            int stones = Integer.parseInt(st.get("stones"));
            List<Integer> mine = ints(st.get("your_cards"));
            int card;
            try {
                card = chooseCard(stones, mine, ints(st.get("opp_cards")));
            } catch (RuntimeException e) {          // a bug in the strategy must not forfeit on time
                System.err.println("[" + name + "] chooseCard threw " + e + "; playing the smallest legal card");
                card = safestCard(stones, mine);
            }
            System.out.println("[" + name + "] " + stones + " stones, playing " + card);
            Object[] mv = call("POST", "/move?format=text", "card=" + card);
            if ((int) mv[0] != 200) {
                System.err.println("[" + name + "] move " + card + " rejected: " + parse((String) mv[1]).getOrDefault("error", (String) mv[1]));
                int fallback = safestCard(stones, mine);
                if (fallback != card) {
                    System.err.println("[" + name + "] playing " + fallback + " instead");
                    mv = call("POST", "/move?format=text", "card=" + fallback);
                }
                if ((int) mv[0] != 200) Thread.sleep(1000);   // not our turn any more: do not hammer the server
            }
        }
    }

    public static void main(String[] args) throws Exception {
        String server = System.getenv().getOrDefault("CARDNIM_SERVER", "http://localhost:8000");
        String game = System.getenv().getOrDefault("CARDNIM_GAME", "");
        String name = System.getenv().getOrDefault("CARDNIM_NAME", "Java bot");
        String seat = System.getenv().getOrDefault("CARDNIM_SEAT", "");
        for (int i = 0; i + 1 < args.length; i += 2) {
            switch (args[i]) {
                case "--server": server = args[i + 1]; break;
                case "--game": game = args[i + 1]; break;
                case "--name": name = args[i + 1]; break;
                case "--seat": seat = args[i + 1]; break;
                default: break;
            }
        }
        if (game.isEmpty()) {
            System.err.println("usage: java Client --server http://host:8000 --game ID --name NAME [--seat 1|2]");
            System.exit(1);
        }
        Client c = new Client(server, game, name);
        c.join(seat);
        System.exit(c.play());
    }
}

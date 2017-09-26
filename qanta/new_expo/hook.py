from display_util import show_score

class Hook:

    def __init__(self, call_every='round'):
        self.call_every = call_every

class GameInterfaceHook(Hook):
    
    def __init__(self, game, call_every='step'):
        self.call_every = call_every
        self.game = game
        
    def run(self):
        print(self.game.round.question.page)
        print('------')
        print(self.game.round.get_clue())
        print('++++++++++++')
        show_score(self.game.scores[0], self.game.scores[1], flush=False)

class NotifyBuzzingHook(Hook):

    def __init__(self, game, call_every='step'):
        self.call_every = call_every
        self.game = game

    def run(self):
        buzzed = self.game.buzzed
        agents = self.game.agents
        for i, agent in enumerate(agents):
            # move each agent to the first
            b = [buzzed[i]] + buzzed[:i] + buzzed[i+1:]
            agent.notify_buzzing(buzzed)

class VisualizeGuesserBuzzer(Hook):

    def __init__(self, guesser_buzzer, call_every='step'):
        self.guesser = guesser_buzzer.guesser
        self.buzzer = guesser_buzzer.buzzer
        self.call_every = call_every

    def run(self):
        print('===== Guesser =====')
        for guess, score in self.guesser.guesses:
            print(guess, score)
        print('===== Buzzer =====')
        print(self.buzzer.ys)

